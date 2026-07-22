import os
import time
import datetime
import asyncio
import subprocess
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# 1. 라이브러리 및 모듈 로드
from utils.logger import logger
from utils.validator import validate_env_variables

# 2. 기동 전 환경변수 무결성 검사
load_dotenv(dotenv_path="env")
validate_env_variables()

# 3. 서비스 아키텍처 로드
from services.config_service import ConfigService
from services.status_service import StatusService
from services.docker_service import DockerService
from services.backup_service import BackupService
from services.monitor_service import MonitorService

from bot.views import ServerControlView, ConfigConfirmView
from bot.embeds import create_dashboard_embed

# 서비스 객체 단일화 생성 (의존성 주입)
config_service = ConfigService()
status_service = StatusService(config_service.base_path)
docker_service = DockerService(config_service)
backup_service = BackupService(config_service, status_service)
monitor_service = MonitorService(config_service, status_service)

EMPTY_BACKUP_DELAY = 900  # 15분 퇴장 감시

class PalworldBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)
        
        # 외부 서비스 주입
        self.config = config_service
        self.status_service = status_service
        self.docker_service = docker_service
        self.backup_service = backup_service
        self.monitor_service = monitor_service
        
        # 봇 내부 상태 캐싱 변수
        self.server_version = "알 수 없음"
        self.last_player_count = -1
        self.last_status = "UNKNOWN"
        self.last_restart_time = 0.0
        self.last_scheduled_restart_date = ""
        
        self.empty_timestamp = 0.0
        self.backup_pending_on_empty = False
        
        # 대시보드 중복 생성 제어용 비동기 락(Lock) 추가
        self.dashboard_lock = asyncio.Lock()

    async def setup_hook(self):
        status_data = self.status_service.get_status()
        self.add_view(ServerControlView(update_available=status_data.get("update_available", False)))
        await self.tree.sync()
        logger.info("디스코드 슬래시 커맨드 트리가 성공적으로 동기화되었습니다.")

    async def on_message(self, message):
        """파일명 예외 처리 및 파일명 출력 디버그가 보완된 핸들러"""
        if message.author.bot:
            return

        if message.guild is None:
            # 첨부된 파일들의 실제 파일명을 모두 로그에 찍어 확인합니다.
            filenames = [att.filename for att in message.attachments] if message.attachments else []
            logger.info(
                f"🚨 [DM 수신 디버그] "
                f"보낸 사람: {message.author.name} | "
                f"디스코드 ID: {message.author.id} | "
                f"등록된 관리자 목록: {self.config.admin_ids} | "
                f"감지된 파일명 목록: {filenames} | "
                f"내용: '{message.content}'"
            )

        # 관리자 검증
        if message.guild is None and message.author.id in self.config.admin_ids:
            if message.attachments:
                for attachment in message.attachments:
                    # 사용자가 실수할 수 있는 다양한 파일명 패턴을 모두 허용합니다.
                    if attachment.filename in [".env", "env", "env.txt", ".env.txt"]:
                        await self.config.apply_dm_env_file(self, message, attachment)
                        return
            
            elif message.content.strip() in [".env", "설정파일", "설정"]:
                await self.config.send_dm_env_file(self, message)
                return

        await self.process_commands(message)

    async def run_maintenance_sequence(self):
        """정기 점검 수동/자동 업데이트 통합 유지 프로세스"""
        channel = self.get_channel(self.config.channel_id)
        temp_messages = []

        # 5분 전 공지
        if channel:
            msg = await channel.send("⚠️ **[서버 정기 점검 알림]** 5분 후에 서버 점검 및 자동 업데이트를 진행합니다.")
            temp_messages.append(msg)
        await self.docker_service.send_rcon("Broadcast Server_will_restart_in_5_minutes")
        await asyncio.sleep(240)

        # 1분 전 공지
        if channel:
            msg = await channel.send("⏰ **[서버 정기 점검 알림]** 점검 시작 1분 전입니다. 안전한 위치에서 로그아웃해 주십시오.")
            temp_messages.append(msg)
        await self.docker_service.send_rcon("Broadcast Server_will_restart_in_60_seconds")
        await asyncio.sleep(30)

        # 30초 전 공지
        if channel:
            msg = await channel.send("🚨 **[서버 정기 점검 알림]** 점검 시작 30초 전입니다.")
            temp_messages.append(msg)
        await self.docker_service.send_rcon("Broadcast Server_will_restart_in_30_seconds")
        await asyncio.sleep(20)

        # 최종 저장
        await self.docker_service.send_rcon("Broadcast Saving_data_and_stopping_NOW")
        await self.docker_service.send_rcon("Save")
        await asyncio.sleep(10)

        # 컨테이너 정지 및 임시 공지 메시지 즉시 제거
        try:
            container = self.docker_service.get_container()
            if container:
                container.stop()
        except Exception as e:
            logger.exception(f"정기 점검 도중 컨테이너 정지 실패 오류: {e}")

        for msg in temp_messages:
            try:
                await msg.delete()
            except Exception:
                pass

        # 백업 수행
        await self.backup_service.run_backup(self)

        # 도커 컴포즈 재기동 및 빌드 ID 업데이트 동기화
        try:
            def _pull_and_recreate():
                subprocess.run(["docker-compose", "pull"], cwd=self.config.base_path, check=True)
                subprocess.run(["docker-compose", "up", "-d", "--force-recreate"], cwd=self.config.base_path, check=True)
            await asyncio.to_thread(_pull_and_recreate)
            
            status_data = self.status_service.get_status()
            if "latest_checked_build" in status_data:
                status_data["known_steam_build"] = status_data["latest_checked_build"]
            status_data["update_available"] = False
            
            self.last_player_count = 0
            self.backup_pending_on_empty = False
            self.empty_timestamp = 0.0
            
            status_data["last_restart"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
            self.status_service.save_status(status_data)

            await self.refresh_dashboard()

            if channel:
                m_fin = await channel.send("✅ 서버 업데이트 및 점검이 정상 완료되었습니다.")
                await m_fin.delete(delay=3600)
        except Exception as e:
            logger.exception("컨테이너 도커 컴포즈 재기동 중 심각한 예외 발생:")
            log_channel = self.get_channel(self.config.log_channel_id)
            if log_channel:
                await log_channel.send(f"❌ **[업데이트 실패]** 컨테이너 업데이트 중 오류가 발생했습니다: {e}")

    async def refresh_dashboard(self):
        """현황판 정보 수집 및 GUI 갱신 기능 (중복 방지 락 및 자가 치유 스캔 적용)"""
        async with self.dashboard_lock:  # 비동기 중복 진입 완전 차단
            channel = self.get_channel(self.config.channel_id)
            if not channel:
                try:
                    channel = await self.fetch_channel(self.config.channel_id)
                except Exception as ch_err:
                    logger.error(f"[CHANNEL ERROR] ID({self.config.channel_id}) 채널 획득 실패: {ch_err}")
                    return

            status, players = "OFFLINE", "0 / 0"
            color = discord.Color.red()
            current_players = 0
            
            container = self.docker_service.get_container()
            if container and container.status == "running":
                state = container.attrs.get("State", {})
                health_status = state.get("Health", {}).get("Status", "none")

                if health_status == "starting":
                    status = "STARTING"
                    players = "로딩 중..."
                    color = discord.Color.orange()
                    self.server_version = "알 수 없음"
                    self.last_player_count = 0
                elif health_status == "unhealthy":
                    status = "UNHEALTHY"
                    players = "0 / 0"
                    color = discord.Color.red()
                    self.server_version = "알 수 없음"
                    self.last_player_count = 0
                else:
                    # API를 통한 세부 상태 조회
                    api_success, version, current_players, max_players = await self.monitor_service.get_server_metrics()
                    if api_success and version != "알 수 없음":
                        status = "ONLINE"
                        color = discord.Color.green()
                        self.server_version = version
                        players = f"{current_players} / {max_players}"
                        self.last_player_count = current_players
                    else:
                        status = "STARTING"
                        players = "로딩 중..."
                        color = discord.Color.orange()
                        self.server_version = "알 수 없음"
                        self.last_player_count = 0
            else:
                self.server_version = "알 수 없음"
                self.last_player_count = 0

            self.last_status = status
            data = self.status_service.get_status()

            # 지능형 이중 동적 자동 백업 로직
            if status == "ONLINE" and current_players >= 1:
                now_dt = datetime.datetime.now()
                required_interval = self.backup_service.get_backup_interval(now_dt)
                last_backup_ts = data.get("last_backup_timestamp", 0.0)
                
                if time.time() - last_backup_ts >= required_interval:
                    await self.backup_service.run_backup(self)
                    data = self.status_service.get_status()

            # 메모리 한계치 초과 시 안전 재기동 조치
            cpu_percent, ram_display, mem_percent = self.monitor_service.get_system_resources()
            if mem_percent > self.config.re_limit:
                current_time = time.time()
                if current_time - self.last_restart_time > 1800:
                    self.last_restart_time = current_time
                    await self.docker_service.send_rcon("Broadcast Server_is_stopping_for_maintenance")
                    await self.docker_service.send_rcon("Save")
                    await asyncio.sleep(5)
                    
                    try:
                        c = self.docker_service.get_container()
                        if c:
                            c.stop()
                        await asyncio.sleep(5)
                        
                        def _recreate():
                            subprocess.run(["docker-compose", "up", "-d", "--force-recreate"], cwd=self.config.base_path, check=True)
                        await asyncio.to_thread(_recreate)
                    except Exception as e:
                        logger.error(f"메모리 초과 자동 재시작 처리 중 오류: {e}")
                    
                    data = self.status_service.get_status()
                    data["last_restart"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
                    self.status_service.save_status(data)

            # UI Embed 생성 함수 분리 적용
            embed = create_dashboard_embed(
                server_name=self.config.server_name,
                server_ip=self.config.server_ip,
                server_port=self.config.server_port,
                status=status,
                players=players,
                cpu_percent=cpu_percent,
                ram_display=ram_display,
                last_backup=data.get('last_backup', '-'),
                last_restart=data.get('last_restart', '-'),
                version=self.server_version,
                color=color
            )

            update_state = data.get("update_available", False)
            view = ServerControlView(update_available=update_state)
            
            msg_id = data.get("msg_id")
            msg = None

            # 1. 저장된 msg_id가 존재할 때 유효성 확인
            if msg_id:
                try:
                    msg = await channel.fetch_message(msg_id)
                except discord.NotFound:
                    logger.info("저장된 ID의 현황판을 채널에서 찾을 수 없습니다. 자가 복구 스캔을 개시합니다.")
                    msg = None
                except Exception as e:
                    logger.error(f"기존 현황판 조회 중 에러 발생: {e}")
                    msg = None

            # 2. 자가 치유(Self-Healing) 알고리즘 적용: msg_id가 없거나 메시지 분실 시 채널 직접 탐색
            if msg is None:
                try:
                    found_dashboard_messages = []
                    async for history_msg in channel.history(limit=50):
                        # 본인이 작성한 메시지 중 대시보드 형식의 임베드 제목 확인
                        if history_msg.author.id == self.user.id and history_msg.embeds:
                            embed_title = history_msg.embeds[0].title
                            if embed_title and (self.config.server_name in embed_title):
                                found_dashboard_messages.append(history_msg)
                    
                    if found_dashboard_messages:
                        # 가장 최근의 메시지를 대시보드로 지정하고 DB 동기화
                        msg = found_dashboard_messages[0]
                        data["msg_id"] = msg.id
                        self.status_service.save_status(data)
                        logger.info(f"채널 기록 스캔을 통해 기존 대시보드를 안정적으로 식별하고 연결했습니다. (ID: {msg.id})")
                        
                        # 중복된 이전 버전의 현황판 메시지는 정리 (청소 정책)
                        for duplicate_msg in found_dashboard_messages[1:]:
                            try:
                                await duplicate_msg.delete()
                                logger.info(f"중복 확인된 옛날 현황판 메시지를 삭제했습니다. (ID: {duplicate_msg.id})")
                            except Exception:
                                pass
                except Exception as scan_err:
                    logger.error(f"현황판 자가 복구를 위한 역사 스캔 실패: {scan_err}")

            # 3. 데이터 송수신 처리 (기존 것 수정 혹은 최초 신규 빌드)
            if msg:
                try:
                    await msg.edit(embed=embed, view=view)
                except Exception as edit_err:
                    logger.error(f"식별된 현황판 수정 실패, 신규 생성을 진행합니다: {edit_err}")
                    msg = None

            if msg is None:
                try:
                    msg = await channel.send(embed=embed, view=view)
                    data["msg_id"] = msg.id
                    self.status_service.save_status(data)
                    logger.info(f"현황판 신규 생성 완료. (ID: {msg.id})")
                except Exception as e:
                    logger.error(f"현황판 최초 발송 및 저장 실패: {e}")

bot = PalworldBot()

# --- 디스코드 슬래시 커맨드 핸들러 정의 ---

@bot.tree.command(name="start", description="팰월드 서버를 시작합니다.")
async def start(interaction: discord.Interaction):
    if interaction.user.id not in bot.config.admin_ids:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    await interaction.response.defer()
    try:
        container = bot.docker_service.get_container()
        if container:
            if container.status == "running":
                msg = await interaction.followup.send("ℹ️ 서버가 이미 실행 중입니다.")
                await msg.delete(delay=900)
            else:
                container.start()
                msg = await interaction.followup.send("✅ 서버 가동을 시작했습니다.")
                await msg.delete(delay=900)
                logger.info("슬래시 명령어로 인해 palworld-server 기동이 시작되었습니다.")
    except Exception as e:
        logger.exception(f"슬래시 명령어 start 실행 도중 에러: {e}")
        await interaction.followup.send(f"❌ 오류 발생: {e}", ephemeral=True)

@bot.tree.command(name="stop", description="팰월드 서버를 안전하게 정지합니다.")
async def stop(interaction: discord.Interaction):
    if interaction.user.id not in bot.config.admin_ids:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    await interaction.response.send_message("🛑 서버를 안전하게 저장 후 정지합니다. (약 10초 소요)")
    orig = await interaction.original_response()
    await orig.delete(delay=900)
    
    await bot.docker_service.send_rcon("Broadcast Server_is_stopping_for_maintenance")
    await bot.docker_service.send_rcon("Save")
    await asyncio.sleep(5)
    try:
        container = bot.docker_service.get_container()
        if container:
            container.stop()
            logger.info("슬래시 명령어로 인해 palworld-server 가 안전 정지되었습니다.")
    except Exception as e:
        logger.exception(f"슬래시 명령어 stop 실행 도중 에러: {e}")
        
    msg2 = await interaction.followup.send("✅ 서버가 정지되었습니다.")
    await msg2.delete(delay=900)

@bot.tree.command(name="restart", description="팰월드 서버를 안전하게 백업 후 재시작합니다.")
async def restart(interaction: discord.Interaction):
    if interaction.user.id not in bot.config.admin_ids:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
        
    await interaction.response.send_message("🔄 서버 재시작 시퀀스 가동 (공지 -> 저장 -> 백업 -> 정지 -> 시작)")
    orig = await interaction.original_response()
    await orig.delete(delay=900)
    
    await bot.docker_service.send_rcon("Broadcast Server_is_stopping_for_maintenance")
    await bot.docker_service.send_rcon("Save")
    await asyncio.sleep(5)
    
    try:
        # 1. 기동 중인 컨테이너에서 세이브 저장 후 정지 전 백업 수행
        try:
            await bot.backup_service.run_backup(bot)
        except Exception as b_err:
            logger.error(f"재시작 전 자동 백업 시도 중 오류 발생 (무시하고 정지 절차 진행): {b_err}")

        # 2. 컨테이너 정지 및 재생성 재기동
        container = bot.docker_service.get_container()
        if container:
            container.stop()
        await asyncio.sleep(5)
        
        bot.last_player_count = 0
        bot.backup_pending_on_empty = False
        bot.empty_timestamp = 0.0
        
        def _reboot():
            subprocess.run(["docker-compose", "up", "-d", "--force-recreate"], cwd=bot.config.base_path, check=True)
        await asyncio.to_thread(_reboot)
        logger.info("슬래시 명령어로 인해 백업 완료 후 palworld-server 가 재시작되었습니다.")
    except Exception as e:
        logger.exception(f"슬래시 명령어 restart 도중 예외: {e}")
        
    bot.last_restart_time = time.time()
    data = bot.status_service.get_status()
    data["last_restart"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    bot.status_service.save_status(data)
    
    msg2 = await interaction.followup.send("✅ 서버가 안전하게 백업 및 재시작되었습니다.")
    await msg2.delete(delay=900)

@bot.tree.command(name="clean", description="고정된 메시지를 제외하고 채널의 모든 메시지를 삭제합니다.")
async def clean_channel(interaction: discord.Interaction):
    if interaction.user.id not in bot.config.admin_ids:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    
    try:
        pins = await channel.pins()
        pinned_ids = {pin.id for pin in pins}
        
        def is_not_pinned(msg):
            return msg.id not in pinned_ids

        deleted = await channel.purge(limit=1000, check=is_not_pinned)
        await interaction.followup.send(f"✅ 고정 메시지를 제외한 {len(deleted)}개의 메시지를 삭제했습니다.", ephemeral=True)
        logger.info(f"clean 명령어로 {len(deleted)}개의 메시지가 제거되었습니다.")
    except Exception as e:
        logger.exception(f"슬래시 명령어 clean 실행 실패: {e}")
        await interaction.followup.send(f"❌ 오류 발생: {e}", ephemeral=True)    

@bot.tree.command(name="backup", description="팰월드 세이브 데이터를 수동으로 즉시 백업합니다.")
async def manual_backup(interaction: discord.Interaction):
    if interaction.user.id not in bot.config.admin_ids:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        success, filename = await bot.backup_service.run_backup(bot)
        if success:
            await interaction.followup.send(f"✅ 수동 백업을 성공적으로 완료했습니다.\n📂 백업 파일: `{filename}`", ephemeral=True)
            logger.info(f"수동 안전 백업 요청이 정상 처리되었습니다: {filename}")
        else:
            await interaction.followup.send("❌ 백업 실패: 파일 압축 도중 오류가 발생했습니다.", ephemeral=True)
    except Exception as e:
        logger.exception(f"수동 백업 도중 오류가 생겼습니다: {e}")
        await interaction.followup.send(f"❌ 오류 발생: {e}", ephemeral=True)

@bot.tree.command(name="디스크", description="현재 서버의 디스크 용량과 상태를 즉시 확인합니다.")
async def check_disk_status(interaction: discord.Interaction):
    if interaction.user.id not in bot.config.admin_ids:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    try:
        disk_percent, used_gb, total_gb = bot.monitor_service.get_disk_usage()
        color = discord.Color.green() if disk_percent < 80.0 else discord.Color.red()
        
        embed = discord.Embed(
            title="💾 서버 디스크 상태 조회",
            color=color,
            timestamp=datetime.datetime.now()
        )
        embed.add_field(name="전체 용량", value=f"`{total_gb:.1f} GB`", inline=True)
        embed.add_field(name="사용 중인 용량", value=f"`{used_gb:.1f} GB`", inline=True)
        embed.add_field(name="사용률", value=f"`{disk_percent}%`", inline=True)
        
        if disk_percent >= 80.0:
            embed.description = "🚨 **경고**: 디스크 사용량이 80%를 초과하여 정리가 필요할 수 있습니다."
        else:
            embed.description = "✅ 디스크 공간이 여유롭고 안전한 상태입니다."
            
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"디스크 조회 명령어 실행 중 오류: {e}")
        await interaction.followup.send(f"❌ 디스크 조회 중 오류가 발생했습니다: {e}", ephemeral=True)

@bot.tree.command(name="설정변경", description="서버 설정(.env) 항목을 일괄 변경하고 서버를 1회 재시작합니다.")
@app_commands.describe(query="형식: KEY1=VALUE1, KEY2=VALUE2 (예: EXP_RATE=2.0, CATCH_RATE=1.5)")
async def change_config(interaction: discord.Interaction, query: str):
    if interaction.user.id not in bot.config.admin_ids:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    
    pairs = [p.strip() for p in query.split(",") if p.strip()]
    parsed_changes = {}
    
    for p in pairs:
        if "=" not in p:
            return await interaction.followup.send(f"❌ 올바른 형식이 아닙니다. '=' 기호를 기준으로 입력해 주세요: `{p}`", ephemeral=True)
        k, v = p.split("=", 1)
        parsed_changes[k.strip()] = v.strip()
        
    current_env = bot.config.read_env_file()
    invalid_keys = []
    changes_to_apply = []
    
    for k, v in parsed_changes.items():
        if k not in current_env:
            invalid_keys.append(k)
        else:
            changes_to_apply.append({
                "key": k,
                "old": current_env[k],
                "new": v
            })
            
    if invalid_keys:
        invalid_str = ", ".join([f"`{x}`" for x in invalid_keys])
        return await interaction.followup.send(f"❌ 존재하지 않는 설정 항목이 포함되어 있습니다: {invalid_str}", ephemeral=True)
        
    if not changes_to_apply:
        return await interaction.followup.send("❌ 변경할 설정 항목이 존재하지 않습니다.", ephemeral=True)
        
    embed = discord.Embed(title="⚙️ 팰월드 설정 일괄 변경 승인 요청", color=discord.Color.blue(), timestamp=datetime.datetime.now())
    desc = "요청하신 설정값을 확인해 주세요. 아래 변경 내용을 반영하고 재시작하시겠습니까?\n\n"
    for c in changes_to_apply:
        desc += f"• **{c['key']}**: `{c['old']}` ➔ `{c['new']}`\n"
    embed.description = desc
    embed.set_footer(text="승인 버튼 클릭 시 env 파일 수정과 함께 서버 자동 재시작이 진행됩니다.")
    
    view = ConfigConfirmView(changes_to_apply, interaction.user.id)
    msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    
    await view.wait()
    
    if view.confirmed:
        update_dict = {c['key']: c['new'] for c in changes_to_apply}
        bot.config.write_env_file(update_dict)
        
        # 시스템 환경변수 동적 동기화
        for c in changes_to_apply:
            os.environ[c['key']] = c['new']
        bot.config.reload_rcon_settings()
                
        embed.title = "⚙️ 설정 반영 및 서버 재기동 시작..."
        embed.color = discord.Color.orange()
        embed.description = "설정이 정상 기입되었습니다. 안전 저장을 진행하고 서버 재배치를 실행합니다."
        await msg.edit(embed=embed, view=None)
        
        channel = bot.get_channel(bot.config.channel_id)
        if channel:
            announce_msg = await channel.send("🔄 **[설정 변경]** 새로운 설정 사항 적용을 위해 서버가 일괄 저장 후 재배치됩니다.")
            await announce_msg.delete(delay=900)
            
        await bot.docker_service.send_rcon("Broadcast Server_is_restarting_to_apply_new_settings")
        await bot.docker_service.send_rcon("Save")
        await asyncio.sleep(5)
        
        try:
            await bot.backup_service.run_backup(bot)
        except Exception as b_err:
            logger.error(f"설정 적용 전 강제 백업 도중 오류 감지: {b_err}")
            
        bot.last_player_count = 0
        bot.backup_pending_on_empty = False
        bot.empty_timestamp = 0.0
        
        try:
            container = bot.docker_service.get_container()
            if container:
                container.stop()
            await asyncio.sleep(5)
            
            def _reboot():
                subprocess.run(["docker-compose", "up", "-d", "--force-recreate"], cwd=bot.config.base_path, check=True)
            await asyncio.to_thread(_reboot)
            
            data = bot.status_service.get_status()
            data["last_restart"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
            bot.status_service.save_status(data)
            
            await bot.refresh_dashboard()
            
            embed.title = "⚙️ 설정 일괄 변경 완료"
            embed.color = discord.Color.green()
            embed.description = "설정값 일괄 대입 및 서버 컨테이너 재배치가 성공적으로 완료되었습니다."
            await msg.edit(embed=embed, view=None)
            logger.info("관리자의 설정 일괄 변경 요청에 따른 재구동 완료.")
        except Exception as err:
            logger.exception("설정 반영 중 예외:")
            embed.title = "❌ 설정 변경 후 재기동 오류 발생"
            embed.color = discord.Color.red()
            embed.description = f"설정 파일은 수정되었으나, 컨테이너 기동 중 에러가 발생했습니다: `{err}`"
            await msg.edit(embed=embed, view=None)

# --- 백그라운드 태스크 루틴 정의 ---

@tasks.loop(seconds=60)
async def dashboard_task():
    try:
        await bot.refresh_dashboard()
    except Exception as e:
        logger.error(f"dashboard_task 루프 내부 오류 감지: {e}")

@tasks.loop(seconds=5)
async def player_watcher_task():
    if not bot.is_ready():
        return

    try:
        container = bot.docker_service.get_container()
        if container and container.status == "running":
            if bot.last_status == "ONLINE":
                api_success, version, current_players, max_players = await bot.monitor_service.get_server_metrics()
                if api_success:
                    # 퇴장 감시 상태 전이 판정
                    if bot.last_player_count >= 1 and current_players == 0:
                        bot.empty_timestamp = time.time()
                        bot.backup_pending_on_empty = True
                        logger.info(f"[퇴장 감시] 마지막 유저 퇴장 감지. {EMPTY_BACKUP_DELAY}초 대기 후 안전 백업 예약.")
                        
                    if current_players >= 1 and bot.backup_pending_on_empty:
                        bot.backup_pending_on_empty = False
                        bot.empty_timestamp = 0.0
                        logger.info("[퇴장 감시] 유저 재접속 확인. 예약된 안전 백업 취소됨.")
                        
                    if bot.backup_pending_on_empty and current_players == 0:
                        elapsed = time.time() - bot.empty_timestamp
                        if elapsed >= EMPTY_BACKUP_DELAY:
                            bot.backup_pending_on_empty = False
                            bot.empty_timestamp = 0.0
                            logger.info(f"[퇴장 감시] 유예 기간 경과로 인한 데이터 백업.")
                            await bot.backup_service.run_backup(bot)
                            await bot.refresh_dashboard()
                    
                    if current_players != bot.last_player_count:
                        await bot.refresh_dashboard()
                else:
                    await bot.refresh_dashboard()
            else:
                await bot.refresh_dashboard()
        else:
            if bot.last_status != "OFFLINE":
                await bot.refresh_dashboard()
    except Exception as e:
        if bot.last_status != "OFFLINE":
            await bot.refresh_dashboard()

@tasks.loop(hours=8)
async def steam_update_checker_task():
    if not bot.is_ready(): 
        return
    has_update, build_id = await bot.monitor_service.check_steam_update()
    if has_update:
        await bot.refresh_dashboard()

@tasks.loop(seconds=10)
async def scheduled_restart_task():
    if not bot.is_ready():
        return
    try:
        now_dt = datetime.datetime.now()
        if now_dt.strftime("%H:%M") == bot.config.restart_time and bot.last_scheduled_restart_date != now_dt.strftime("%Y-%m-%d"):
            bot.last_scheduled_restart_date = now_dt.strftime("%Y-%m-%d")
            await bot.monitor_service.check_steam_update()
            await bot.run_maintenance_sequence()
    except Exception as e:
        logger.error(f"scheduled_restart_task 루프 중 예외 발생: {e}")

@tasks.loop(hours=24)
async def scheduled_cleanup_task():
    try:
        await bot.backup_service.delete_old_backups(bot)
    except Exception as e:
        logger.error(f"scheduled_cleanup_task 루프 도중 예외 발생: {e}")

@tasks.loop(hours=12)
async def scheduled_disk_check_task():
    """12시간마다 시스템 디스크 잔량을 조회하여 80% 이상 초과 시 경고 메시지를 로그 채널에 전송합니다."""
    if not bot.is_ready():
        return
    try:
        disk_percent, used_gb, total_gb = bot.monitor_service.get_disk_usage()
        if disk_percent >= 80.0:
            log_channel = bot.get_channel(bot.config.log_channel_id)
            if log_channel:
                embed = discord.Embed(
                    title="⚠️ [디스크 용량 경고]",
                    description=f"서버의 디스크 사용량이 임계치(80%)를 초과했습니다. 원활한 구동을 위해 용량 정리를 고려해 주십시오.",
                    color=discord.Color.red(),
                    timestamp=datetime.datetime.now()
                )
                embed.add_field(name="현재 사용률", value=f"`{disk_percent}%`", inline=True)
                embed.add_field(name="상세 점유율", value=f"`{used_gb:.1f} GB` / `{total_gb:.1f} GB`", inline=True)
                await log_channel.send(embed=embed)
                logger.warning(f"디스크 사용량 임계 초과 감지: {disk_percent}% ({used_gb:.1f}GB/{total_gb:.1f}GB)")
    except Exception as e:
        logger.error(f"scheduled_disk_check_task 루프 내 예외 발생: {e}")

@bot.event
async def on_ready():
    logger.info(f"봇 로그인 완료: {bot.user.name}")
    try:
        await bot.fetch_channel(bot.config.channel_id)
        await bot.fetch_channel(bot.config.log_channel_id)
    except Exception as e:
        logger.warning(f"초기 채널 캐싱 오류 (무시 가능): {e}")

    await bot.monitor_service.check_steam_update()

    if not dashboard_task.is_running(): dashboard_task.start()
    if not player_watcher_task.is_running(): player_watcher_task.start()
    if not steam_update_checker_task.is_running(): steam_update_checker_task.start()
    if not scheduled_restart_task.is_running(): scheduled_restart_task.start()
    if not scheduled_cleanup_task.is_running(): scheduled_cleanup_task.start()
    if not scheduled_disk_check_task.is_running(): scheduled_disk_check_task.start()

bot.run(bot.config.token)