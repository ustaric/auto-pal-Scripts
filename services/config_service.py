import os
import datetime
import asyncio
import subprocess
import discord  # discord.File 객체 사용을 위해 로드
from utils.logger import logger

class ConfigService:
    def __init__(self):
        self.token = os.getenv("DISCORD_TOKEN")
        
        admin_raw = os.getenv("ADMIN_IDS", "")
        self.admin_ids = [int(x.strip()) for x in admin_raw.split(",") if x.strip()]
        
        channel_id_raw = os.getenv("CHANNEL_ID")
        self.channel_id = int(channel_id_raw) if channel_id_raw else None
        
        self.log_channel_id = int(os.getenv("LOG_CHANNEL_ID", 1255145931255582770))
        self.server_name = os.getenv("SERVER_NAME")
        self.server_ip = os.getenv("SERVER_IP")
        self.server_port = os.getenv("SERVER_PORT")
        self.rcon_pwd = os.getenv("ADMIN_PASSWORD")
        
        rcon_port_raw = os.getenv("RCON_PORT")
        self.rcon_port = int(rcon_port_raw) if rcon_port_raw else None
        self.api_port = os.getenv("REST_API_PORT")
        self.save_data_path = os.getenv("SAVE_DATA_PATH")
        self.backup_path = os.getenv("BACKUP_PATH")
        self.base_path = os.getenv("BASE_PATH")
        self.re_limit = int(os.getenv("MEMORY_RESTART_THRESHOLD", 80))
        self.retention_days = int(os.getenv("BACKUP_RETENTION_DAYS", 2))
        self.restart_time = os.getenv("RESTART_TIME", "04:00")

    def reload_rcon_settings(self):
        """설정 변경 후 내부 메모리 상의 변수를 강제 동기화합니다."""
        self.rcon_pwd = os.getenv("ADMIN_PASSWORD")
        rcon_port_raw = os.getenv("RCON_PORT")
        self.rcon_port = int(rcon_port_raw) if rcon_port_raw else None
        self.api_port = os.getenv("REST_API_PORT")

    def _parse_env_file_path(self, file_path):
        """지정된 경로의 환경설정 파일을 딕셔너리로 구문 분석합니다."""
        settings = {}
        if not os.path.exists(file_path):
            return settings
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        settings[k.strip()] = v.strip()
        except Exception as e:
            logger.error(f"환경설정 파일 파싱 실패 ({file_path}): {e}")
        return settings

    def read_env_file(self):
        env_path = os.path.join(self.base_path, ".env") if self.base_path else ".env"
        return self._parse_env_file_path(env_path)

    def write_env_file(self, changes):
        env_path = os.path.join(self.base_path, ".env") if self.base_path else ".env"
        if not os.path.exists(env_path):
            return False
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            
            new_lines = []
            updated_keys = set()
            
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, v = stripped.split("=", 1)
                    k = k.strip()
                    if k in changes:
                        new_lines.append(f"{k}={changes[k]}\n")
                        updated_keys.add(k)
                        continue
                new_lines.append(line)
                
            for k, v in changes.items():
                if k not in updated_keys:
                    new_lines.append(f"{k}={v}\n")
                    
            with open(env_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            return True
        except Exception as e:
            logger.exception(f".env 설정 파일에 쓰기 작업을 수행하던 도중 에러가 발생했습니다: {e}")
            return False

    async def send_dm_env_file(self, bot, message):
        """관리자에게 가상 서버에 설정된 현재의 .env 파일을 개인 메시지(DM) 첨부 파일로 전송합니다."""
        env_path = os.path.join(self.base_path, ".env")
        
        if not os.path.exists(env_path):
            logger.warning(f"서버에서 설정 파일을 찾을 수 없습니다: {env_path}")
            return await message.channel.send("❌ 가상 서버 경로에 현재 적용된 설정 파일(.env)이 존재하지 않습니다.")

        try:
            await message.channel.send("📥 **현재 가상 서버의 .env 환경 설정 파일을 준비 중입니다...**")
            
            # 물리 주소 상의 .env 파일을 디스코드 첨부파일 객체로 변환하여 다이렉트 송신합니다.
            discord_file = discord.File(env_path, filename=".env")
            await message.channel.send(
                content="📄 **현재 가상 서버에 실제로 적용되어 상시 기동 중인 .env 파일입니다.**\n"
                        "이 파일을 스마트폰(모바일)이나 PC 환경에서 적합하게 수정하신 뒤, "
                        "다시 이 DM 창에 그대로 드래그 앤 드롭 업로드해 주시면 변경점 대조 후 무중단 재기동이 연동됩니다.",
                file=discord_file
            )
            logger.info(f"관리자({message.author.name})에게 DM으로 설정 파일(.env) 파일 전송 완수.")
        except Exception as err:
            logger.exception("DM으로 설정 파일(.env) 발송 시도 도중 서버 예외 발생:")
            await message.channel.send(f"❌ 설정 파일을 포장하여 전송하는 도중 시스템 오류가 발생했습니다: `{err}`")

    async def apply_dm_env_file(self, bot, message, attachment):
        """1:1 DM으로 수신된 .env 파일을 검증 및 안전 백업 후 적용하고 서버를 재배치합니다."""
        await message.channel.send("⚙️ **새로운 설정 파일(.env)이 수신되었습니다.** 자가 분석을 개시합니다...")
        
        env_path = os.path.join(self.base_path, ".env")
        bak_path = env_path + ".bak"
        
        # 1. 파일 변경 전 기존 .env 설정 로드
        old_settings = self._parse_env_file_path(env_path)
        
        try:
            # 만약의 기동 실패 사태를 대비해 기존 .env 파일을 임시 백업 보관합니다.
            if os.path.exists(env_path):
                if os.path.exists(bak_path):
                    os.remove(bak_path)
                os.rename(env_path, bak_path)
            
            # 첨부된 새로운 .env 파일을 다운로드 및 덮어쓰기 합니다.
            await attachment.save(env_path)
            logger.info(f"관리자({message.author.name})가 DM으로 전송한 .env 파일이 성공적으로 수집되었습니다.")
        except Exception as file_err:
            logger.exception("수신된 설정 파일 저장 중 서버 에러:")
            if os.path.exists(bak_path):
                os.rename(bak_path, env_path) # 실패 시 원상복구
            return await message.channel.send(f"❌ 설정 파일을 디스크에 저장하는 과정에서 실패했습니다: `{file_err}`")

        # 2. 파일 변경 후 새로운 .env 설정 로드
        new_settings = self._parse_env_file_path(env_path)

        # 3. 이전 설정과 대조하여 변경된 값만 정밀 검출
        changes = []
        all_keys = set(old_settings.keys()) | set(new_settings.keys())

        for k in sorted(all_keys):
            old_val = old_settings.get(k)
            new_val = new_settings.get(k)
            
            if old_val != new_val:
                if old_val is None:
                    changes.append(f"• **{k}**: `(신규 추가)` ➔ `{new_val}`")
                elif new_val is None:
                    changes.append(f"• **{k}**: `{old_val}` ➔ `(삭제됨)`")
                else:
                    changes.append(f"• **{k}**: `{old_val}` ➔ `{new_val}`")

        # 4. 실질적인 변경점이 없는 경우, 재부팅하지 않고 시퀀스를 무중단 조기 종료
        if not changes:
            if os.path.exists(bak_path):
                os.remove(env_path)
                os.rename(bak_path, env_path) # 백업 복원
            return await message.channel.send("ℹ️ **안내**: 업로드된 파일이 기존 서버 설정과 동일합니다. 변경된 항목이 존재하지 않아 서버 재시작을 취소합니다.")

        # 5. 변경 세부 내역 DM 채널에 임베드로 피드백 전송
        changes_text = "\n".join(changes)
        embed = discord.Embed(
            title="⚙️ .env 설정 변경 감지 내역",
            description=f"다음 항목들이 수정되었습니다. 적용 및 서버 재기동을 속행합니다.\n\n{changes_text}",
            color=discord.Color.blue(),
            timestamp=datetime.datetime.now()
        )
        embed.set_footer(text="설정이 기입된 후 자동 백업 프로세스가 연동됩니다.")
        await message.channel.send(embed=embed)

        # 봇 메모리 상의 RCON 정보 강제 동기화
        self.reload_rcon_settings()

        await message.channel.send("🔄 **서버 무중단 재배치 시퀀스를 기동합니다.**\n• 단계: RCON 세이브 저장 ➔ 백업 생성 ➔ 컨테이너 재빌드")
        
        # 인게임 저장 공지 및 강제 저장 수행
        await bot.docker_service.send_rcon("Broadcast Server_is_restarting_to_apply_new_settings")
        await bot.docker_service.send_rcon("Save")
        await asyncio.sleep(5)

        # 백업 진행 (Rclone 구글 드라이브 동기화 연동)
        try:
            await bot.backup_service.run_backup(bot)
        except Exception as b_err:
            logger.error(f"설정 적용 전 백업 실패 (프로세스는 중단하지 않고 계속 진행): {b_err}")
            await message.channel.send("⚠️ 경고: 자동 백업 생성을 실패했으나, 롤백 안전장치가 확보되어 정리를 계속 이행합니다.")

        # 컨테이너 완전 정지 및 재생성 재기동
        try:
            container = bot.docker_service.get_container()
            if container:
                container.stop()
            await asyncio.sleep(5)

            def _reboot():
                subprocess.run(["docker-compose", "up", "-d", "--force-recreate"], cwd=self.base_path, check=True)
            await asyncio.to_thread(_reboot)

            # 상태 갱신 및 캐시 저장
            data = bot.status_service.get_status()
            data["last_restart"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
            bot.status_service.save_status(data)

            # 실시간 현황판 반영 및 복구 캐시 정리
            await bot.refresh_dashboard()

            # 정상 기동 완료 시 임시 백업본 소거
            if os.path.exists(bak_path):
                os.remove(bak_path)

            logger.info("DM 설정 파일 교체 및 서버 force-recreate 재구동 성공.")
            
            # 성공 결과 DM 전송 및 변경 내역 한 번 더 환기
            success_embed = discord.Embed(
                title="✅ 서버 설정 적용 완료",
                description="새로운 서버 환경 설정(.env) 적용 및 물리 컨테이너 재배치 기동이 정상 완수되었습니다.",
                color=discord.Color.green(),
                timestamp=datetime.datetime.now()
            )
            success_embed.add_field(name="반영 완료된 항목 목록", value=changes_text, inline=False)
            await message.channel.send(embed=success_embed)
            
        except Exception as err:
            logger.exception("설정 반영 도중 컨테이너 재생성 오류 감지:")
            # 기동 크래시 발생 시 즉각 기존 .env 복원
            if os.path.exists(bak_path):
                if os.path.exists(env_path):
                    os.remove(env_path)
                os.rename(bak_path, env_path)
            await message.channel.send(f"❌ **실패**: 컨테이너 물리 재배치 중 치명적인 에러가 보고되었습니다: `{err}`\n⚠️ 안전을 위해 기존 정상 작동되던 설정 파일(.env)로 자동 롤백 복구되었습니다.")