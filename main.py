import discord
from discord import app_commands
from discord.ext import commands, tasks
import os, json, psutil, docker, requests, tarfile, datetime, asyncio, time, subprocess
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from mcrcon import MCRcon

# ==========================================
# 1. 로깅 라이브러리 구축 (용량 무제한 고갈 장애 방지)
# ==========================================
logger = logging.getLogger("palworld_bot")
logger.setLevel(logging.INFO)

# 로그 포맷 규칙 설정 (날짜 - 시간 - 로그레벨 - 메시지)
formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# 콘솔 출력용 핸들러 등록
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# 파일 자동 순환 저장용 핸들러 등록 (최대 5MB, 최대 3개 보존)
file_handler = RotatingFileHandler("palworld_bot.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)


# --- 설정 로드 (봇 구동 전용 env 파일) ---
load_dotenv(dotenv_path="env")
TOKEN = os.getenv("DISCORD_TOKEN")

# 여러 개의 관리자 ID를 쉼표로 분리하여 리스트로 변환합니다.
admin_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_raw.split(",") if x.strip()]

CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")
CHANNEL_ID = int(CHANNEL_ID_RAW) if CHANNEL_ID_RAW else None
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", 1255145931255582770))

SERVER_NAME = os.getenv("SERVER_NAME")
SERVER_IP = os.getenv("SERVER_IP")
SERVER_PORT = os.getenv("SERVER_PORT")
RCON_PWD = os.getenv("ADMIN_PASSWORD")
RCON_PORT_RAW = os.getenv("RCON_PORT")
RCON_PORT = int(RCON_PORT_RAW) if RCON_PORT_RAW else None
API_PORT = os.getenv("REST_API_PORT")
SAVE_DATA_PATH = os.getenv("SAVE_DATA_PATH")
BACKUP_PATH = os.getenv("BACKUP_PATH")
BASE_PATH = os.getenv("BASE_PATH")
RE_LIMIT = int(os.getenv("MEMORY_RESTART_THRESHOLD", 80))
RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", 2))
RESTART_TIME = os.getenv("RESTART_TIME", "04:00")

# --- [퇴장 감시 백업 설정] ---
EMPTY_BACKUP_DELAY = 900  # 마지막 유저가 나가고 비어있는 대기 시간 (15분 = 900초)


# ==========================================
# 2. 기동 시 환경변수 안전 무결성 검증 (Fail-Fast)
# ==========================================
def validate_env_variables():
    logger.info("서버 시작 전 환경변수 안전 무결성 검증을 실시합니다...")
    errors = []
    
    # 포트 범위 및 정수 타입 유효성 검증 함수
    def check_port(val, name):
        try:
            p = int(val)
            if not (1 <= p <= 65535):
                errors.append(f"{name} 은(는) 1에서 65535 사이의 유효한 포트 범위여야 합니다. (입력값: {val})")
        except (ValueError, TypeError):
            errors.append(f"{name} 은(는) 숫자 정수 형식이어야 합니다. (입력값: {val})")

    if not TOKEN:
        errors.append("DISCORD_TOKEN 이 env 설정 파일에 누락되었습니다.")
    if not CHANNEL_ID:
        errors.append("CHANNEL_ID 가 env 설정 파일에 누락되었습니다.")
    if not BASE_PATH:
        errors.append("BASE_PATH 가 env 설정 파일에 누락되었습니다.")
    elif not os.path.exists(BASE_PATH):
        errors.append(f"BASE_PATH 로 지정된 경로가 실제 서버 파일 시스템에 존재하지 않습니다: {BASE_PATH}")
        
    check_port(RCON_PORT, "RCON_PORT")
    check_port(API_PORT, "REST_API_PORT")
    
    try:
        int(RE_LIMIT)
    except (ValueError, TypeError):
        errors.append(f"MEMORY_RESTART_THRESHOLD 는 숫자 형식이어야 합니다. (입력값: {RE_LIMIT})")
        
    try:
        int(RETENTION_DAYS)
    except (ValueError, TypeError):
        errors.append(f"BACKUP_RETENTION_DAYS 는 숫자 형식이어야 합니다. (입력값: {RETENTION_DAYS})")

    if errors:
        for err in errors:
            logger.error(f"[환경변수 무결성 검증 실패] {err}")
        logger.critical("설정값에 중대한 에러가 발견되어 디스코드 봇의 기동을 전면 안전 중단합니다.")
        raise ValueError("환경변수 검증 실패. env 및 .env 파일의 비정상적인 값을 올바르게 조율해 주세요.")
    
    logger.info("모든 환경변수 검증이 안전하게 완료되었습니다. 정상 기동으로 진입합니다.")

# 최초 실행 시 환경변수 자가 진단 실행
validate_env_variables()


# --- 유틸리티 함수 ---
def get_status():
    path = os.path.join(BASE_PATH, "status.json")
    try:
        with open(path, "r") as f: 
            return json.load(f)
    except Exception as e: 
        logger.warning(f"status.json 파일을 읽지 못해 기본 객체를 로드합니다 (최초 실행 중이거나 파일이 깨졌을 수 있음): {e}")
        return {
            "last_backup": "-", 
            "last_cleanup": "-", 
            "last_restart": "-", 
            "msg_id": None,
            "last_backup_timestamp": 0.0,
            "known_steam_build": "0",
            "update_available": False
        }

def save_status(data):
    path = os.path.join(BASE_PATH, "status.json")
    try:
        with open(path, "w") as f: 
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.exception(f"status.json 저장 도중 예외가 기록되었습니다: {e}")

async def send_rcon(command):
    try:
        with MCRcon("127.0.0.1", RCON_PWD, port=RCON_PORT) as mcr:
            return mcr.command(command)
    except Exception as e: 
        logger.error(f"MCRcon 연결 혹은 명령 실행 실패 (커맨드: {command}): {e}")
        return "RCON_ERROR"

async def check_steam_update():
    """스팀 API를 통해 팰월드(AppID: 2394010)의 최신 빌드 정보를 확인합니다."""
    try:
        res = requests.get("https://api.steamcmd.net/v1/info/2394010", timeout=8)
        if res.status_code == 200:
            data = res.json()
            latest_build = data.get("data", {}).get("2394010", {}).get("depots", {}).get("branches", {}).get("public", {}).get("buildid")
            if latest_build:
                status_data = get_status()
                old_build = status_data.get("known_steam_build", "0")
                
                if old_build != "0" and str(old_build) != str(latest_build):
                    status_data["update_available"] = True
                status_data["latest_checked_build"] = str(latest_build)
                save_status(status_data)
                return status_data["update_available"], latest_build
    except Exception as e:
        logger.error(f"스팀 업데이트 정보 대조 통신 중 에러 감지: {e}")
    return False, None

async def run_backup():
    """서버 데이터를 백업하고 지정된 로그 채널로 알림을 보냅니다."""
    now = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    target = f"{BACKUP_PATH}/{now}.tar.gz"
    try:
        # os.system 대신 subprocess.run과 인자 리스트 방식으로 완전히 전향 (쉘 공격 차단)
        subprocess.run(
            ["docker", "exec", "palworld-server", "backup"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        with tarfile.open(target, "w:gz") as tar:
            tar.add(SAVE_DATA_PATH, arcname="Saved")
        
        data = get_status()
        data["last_backup"] = f"{now}.tar.gz"
        data["last_backup_timestamp"] = time.time()
        save_status(data)
        
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"📂 **[백업 성공]** 세이브 파일 백업 완료.\n📄 파일명: `{now}.tar.gz`")
        logger.info(f"정기 백업 저장이 완수되었습니다: {now}.tar.gz")
        return True, f"{now}.tar.gz"
    except Exception as e:
        logger.exception("백업 데이터 파일 압축 및 생성 중 치명적인 예외가 보고되었습니다:")
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"❌ **[백업 실패]** 백업 진행 중 오류 발생: `{e}`")
        return False, None

async def delete_old_backups():
    now = time.time()
    count = 0
    deleted_files = []
    try:
        for f in os.listdir(BACKUP_PATH):
            f_path = os.path.join(BACKUP_PATH, f)
            if os.stat(f_path).st_mtime < now - (RETENTION_DAYS * 86400):
                os.remove(f_path)
                count += 1
                deleted_files.append(f)
        if count > 0:
            data = get_status()
            data["last_cleanup"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
            save_status(data)
            
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel: 
                files_list = "\n".join([f"- `{x}`" for x in deleted_files])
                await log_channel.send(
                    f"🧹 **[백업 정리 완료]** 보존 기간({RETENTION_DAYS}일)이 지난 오래된 백업 파일 {count}개를 삭제했습니다.\n"
                    f"🗑️ **삭제 목록:**\n{files_list}"
                )
            logger.info(f"보존 기한이 초과된 구버전 백업 {count}개가 자동 디스크 정리되었습니다.")
    except Exception as e:
        logger.exception(f"오래된 백업 디스크 정리 중 실패 오류: {e}")

def get_backup_interval(now_dt):
    weekday = now_dt.weekday()
    hour = now_dt.hour
    if hour < 2:
        weekday = (weekday - 1) % 7
        hour += 24
    if weekday <= 4:
        if 19 <= hour < 26: return 30 * 60
    else:
        if 9 <= hour < 26: return 30 * 60
    return 60 * 60

# 설정 파일(.env) 읽기 및 쓰기 헬퍼 함수 (게임 설정은 .env 경로 유지)
def read_env_file():
    env_path = os.path.join(BASE_PATH, ".env") if BASE_PATH else ".env"
    settings = {}
    if not os.path.exists(env_path):
        return settings
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    settings[k.strip()] = v.strip()
    except Exception as e:
        logger.exception(f".env 설정 파일을 가져오는 도중 에러가 생겼습니다: {e}")
    return settings

def write_env_file(changes):
    env_path = os.path.join(BASE_PATH, ".env") if BASE_PATH else ".env"
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

# --- 공용 점검 및 업데이트 시퀀스 ---
async def run_maintenance_sequence():
    """5분, 1분, 30초 전 공지 후 서버를 백업 및 정지하고 업데이트를 실행합니다."""
    channel = bot.get_channel(CHANNEL_ID)
    temp_messages = []

    # 1. 5분 전 공지
    if channel:
        msg = await channel.send("⚠️ **[서버 정기 점검 알림]** 5분 후에 서버 점검 및 자동 업데이트를 진행합니다.")
        temp_messages.append(msg)
    await send_rcon("Broadcast Server_will_restart_in_5_minutes")
    await asyncio.sleep(240)  # 4분 대기

    # 2. 1분 전 공지
    if channel:
        msg = await channel.send("⏰ **[서버 정기 점검 알림]** 점검 시작 1분 전입니다. 안전한 위치에서 로그아웃해 주십시오.")
        temp_messages.append(msg)
    await send_rcon("Broadcast Server_will_restart_in_60_seconds")
    await asyncio.sleep(30)  # 30초 대기

    # 3. 30초 전 공지
    if channel:
        msg = await channel.send("🚨 **[서버 정기 점검 알림]** 점검 시작 30초 전입니다.")
        temp_messages.append(msg)
    await send_rcon("Broadcast Server_will_restart_in_30_seconds")
    await asyncio.sleep(20)  # 20초 대기

    # 4. 최종 경고 및 데이터 저장
    await send_rcon("Broadcast Saving_data_and_stopping_NOW")
    await send_rcon("Save")
    await asyncio.sleep(10)

    # 5. 서버 컨테이너 정지 및 임시 알림 메시지 삭제
    try:
        container = docker_client.containers.get("palworld-server")
        container.stop()
    except Exception as e:
        logger.exception(f"정기 점검 도중 palworld-server 도커 컨테이너 정지 실패 오류: {e}")

    # 컨테이너 정지 직후 알림 메시지 일괄 정리
    for msg in temp_messages:
        try:
            await msg.delete()
        except Exception:
            pass

    # 6. 정기 백업 수행 (로그 채널로만 전송)
    await run_backup()

    # 7. 업데이트 및 컨테이너 재기동 (docker-compose 하이픈 표기형, cwd=BASE_PATH 안전화)
    try:
        subprocess.run(
            ["docker-compose", "pull"],
            cwd=BASE_PATH,
            check=True
        )
        subprocess.run(
            ["docker-compose", "up", "-d", "--force-recreate"],
            cwd=BASE_PATH,
            check=True
        )
        
        # 빌드 ID 동기화 및 업데이트 여부 플래그 초기화
        status_data = get_status()
        if "latest_checked_build" in status_data:
            status_data["known_steam_build"] = status_data["latest_checked_build"]
        status_data["update_available"] = False
        
        # 정기 점검 재시작 시 퇴장 감시 백업 예약 강제 초기화 (중복 방지)
        bot.last_player_count = 0
        bot.backup_pending_on_empty = False
        bot.empty_timestamp = 0.0
        
        status_data["last_restart"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        save_status(status_data)

        # 재기동 완료 후 현황판 즉시 갱신 (버튼 상태 비활성화 반영)
        await refresh_dashboard()

        # 재기동 완료 후 채널 일시 공지 (1시간 후 자동 제거)
        if channel:
            m_fin = await channel.send("✅ 서버 업데이트 및 점검이 정상 완료되었습니다.")
            await m_fin.delete(delay=3600)
    except Exception as e:
        logger.exception("컨테이너 도커 컴포즈 재기동 중 심각한 예외 발생:")
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"❌ **[업데이트 실패]** 컨테이너 업데이트 중 오류가 발생했습니다: {e}")

# --- 서버 제어 뷰 ---
class ServerControlView(discord.ui.View):
    def __init__(self, update_available=False):
        super().__init__(timeout=None)
        
        # 업데이트 가능 여부에 따라 버튼 비주얼 동적 설정
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == "btn_update":
                if update_available:
                    child.disabled = False
                    child.style = discord.ButtonStyle.green
                    child.label = "서버 업데이트 수동 실행"
                else:
                    child.disabled = True
                    child.style = discord.ButtonStyle.secondary
                    child.label = "최신 버전 (업데이트 없음)"

    @discord.ui.button(label="서버 업데이트", style=discord.ButtonStyle.secondary, custom_id="btn_update")
    async def update_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_IDS:
            return await interaction.response.send_message("❌ 이 버튼은 지정된 관리자만 사용할 수 있습니다.", ephemeral=True)
        
        await interaction.response.send_message("🚀 수동 점검 및 업데이트 시퀀스를 가동합니다. (5분 카운트다운 시작)", ephemeral=True)
        await run_maintenance_sequence()

# --- 설정 변경 이중 확인용 뷰 ---
class ConfigConfirmView(discord.ui.View):
    def __init__(self, changes, user_id):
        super().__init__(timeout=60)
        self.changes = changes
        self.user_id = user_id
        self.confirmed = False

    @discord.ui.button(label="변경 승인 및 재시작", style=discord.ButtonStyle.green, custom_id="btn_confirm_config")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ 명령어를 입력한 관리자만 승인할 수 있습니다.", ephemeral=True)
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="변경 취소", style=discord.ButtonStyle.red, custom_id="btn_cancel_config")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("❌ 명령어를 입력한 관리자만 취소할 수 있습니다.", ephemeral=True)
        await interaction.response.send_message("❌ 설정 변경 취소됨. 파일이 수정되지 않았습니다.", ephemeral=True)
        self.stop()

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)
        self.server_version = "알 수 없음"
        self.last_player_count = -1
        self.last_status = "UNKNOWN"
        self.last_restart_time = 0.0
        self.last_scheduled_restart_date = ""
        
        # --- [퇴장 감시 백업용 상태 변수] ---
        self.empty_timestamp = 0.0
        self.backup_pending_on_empty = False

    async def setup_hook(self):
        # 실행 시 상태 정보 파일의 기존 상태를 기반으로 영구 뷰 초기화
        status_data = get_status()
        self.add_view(ServerControlView(update_available=status_data.get("update_available", False)))
        await self.tree.sync()
        logger.info("디스코드 슬래시 커맨드 트리가 성공적으로 동기화되었습니다.")

bot = MyBot()
docker_client = docker.from_env()

# --- 슬래시 명령어 (각 메시지 전송 부에 15분 뒤 자동 삭제 기능 적용) ---
@bot.tree.command(name="start", description="팰월드 서버를 시작합니다.")
async def start(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    await interaction.response.defer()
    try:
        container = docker_client.containers.get("palworld-server")
        if container.status == "running":
            msg = await interaction.followup.send("ℹ️ 서버가 이미 실행 중입니다.")
            await msg.delete(delay=900)  # 15분 후 자동 삭제
        else:
            container.start()
            msg = await interaction.followup.send("✅ 서버 가동을 시작했습니다.")
            await msg.delete(delay=900)  # 15분 후 자동 삭제
            logger.info("슬래시 명령어로 인해 palworld-server 기동이 시작되었습니다.")
    except Exception as e:
        logger.exception(f"슬래시 명령어 start 실행 도중 치명적 에러: {e}")
        await interaction.followup.send(f"❌ 오류 발생: {e}", ephemeral=True)

@bot.tree.command(name="stop", description="팰월드 서버를 안전하게 정지합니다.")
async def stop(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    await interaction.response.send_message("🛑 서버를 안전하게 저장 후 정지합니다. (약 10초 소요)")
    orig = await interaction.original_response()
    await orig.delete(delay=900)  # 시작 안내 메시지 15분 후 자동 삭제
    
    await send_rcon("Broadcast Server_is_stopping_for_maintenance")
    await send_rcon("Save")
    await asyncio.sleep(5)
    try:
        container = docker_client.containers.get("palworld-server")
        container.stop()
        logger.info("슬래시 명령어로 인해 palworld-server 가 안전 정지되었습니다.")
    except Exception as e:
        logger.exception(f"슬래시 명령어 stop 실행 도중 에러: {e}")
        
    msg2 = await interaction.followup.send("✅ 서버가 정지되었습니다.")
    await msg2.delete(delay=900)  # 완료 메시지 15분 후 자동 삭제

@bot.tree.command(name="restart", description="팰월드 서버를 안전하게 재시작합니다.")
async def restart(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
        
    await interaction.response.send_message("🔄 서버 재시작 시퀀스 가동 (공지 -> 저장 -> 정지 -> 시작)")
    orig = await interaction.original_response()
    await orig.delete(delay=900)  # 시작 안내 메시지 15분 후 자동 삭제
    
    await send_rcon("Broadcast Server_is_stopping_for_maintenance")
    await send_rcon("Save")
    await asyncio.sleep(5)
    try:
        container = docker_client.containers.get("palworld-server")
        container.stop()
        await asyncio.sleep(5)
        
        # 수동 재시작 시 퇴장 감시 백업 예약 강제 초기화 (중복 방지)
        bot.last_player_count = 0
        bot.backup_pending_on_empty = False
        bot.empty_timestamp = 0.0
        
        # docker-compose up 재기동 처리 적용 (안전한 subprocess.run 사용)
        subprocess.run(
            ["docker-compose", "up", "-d", "--force-recreate"],
            cwd=BASE_PATH,
            check=True
        )
        logger.info("슬래시 명령어로 인해 palworld-server 가 재시작되었습니다.")
    except Exception as e:
        logger.exception(f"슬래시 명령어 restart 도중 예외가 발생했습니다: {e}")
        
    bot.last_restart_time = time.time()
    data = get_status()
    data["last_restart"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    save_status(data)
    
    msg2 = await interaction.followup.send("✅ 서버가 다시 시작되었습니다.")
    await msg2.delete(delay=900)  # 완료 메시지 15분 후 자동 삭제

@bot.tree.command(name="clean", description="고정된 메시지를 제외하고 채널의 모든 메시지를 삭제합니다.")
async def clean_channel(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
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
        logger.info(f"clean 명령어로 {len(deleted)}개의 무효 메시지가 제거되었습니다.")
    except Exception as e:
        logger.exception(f"슬래시 명령어 clean 실행 실패: {e}")
        await interaction.followup.send(f"❌ 오류 발생: {e}", ephemeral=True)    

@bot.tree.command(name="backup", description="팰월드 세이브 데이터를 수동으로 즉시 백업합니다.")
async def manual_backup(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        success, filename = await run_backup()
        if success:
            await interaction.followup.send(f"✅ 수동 백업을 성공적으로 완료했습니다.\n📂 백업 파일: `{filename}`", ephemeral=True)
            logger.info(f"수동 안전 백업 요청이 정상 처리되었습니다: {filename}")
        else:
            await interaction.followup.send("❌ 백업 실패: 파일 압축 도중 오류가 발생했습니다.", ephemeral=True)
    except Exception as e:
        logger.exception(f"수동 백업 도중 오류가 생겼습니다: {e}")
        await interaction.followup.send(f"❌ 오류 발생: {e}", ephemeral=True)

# --- 설정 일괄 변경 명령어 ---
@bot.tree.command(name="설정변경", description="서버 설정(.env) 항목을 일괄 변경하고 서버를 1회 재시작합니다.")
@app_commands.describe(query="형식: KEY1=VALUE1, KEY2=VALUE2 (예: EXP_RATE=2.0, CATCH_RATE=1.5)")
async def change_config(interaction: discord.Interaction, query: str):
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    
    # 1. 쉼표(,)를 구분자로 입력값 분할
    pairs = [p.strip() for p in query.split(",") if p.strip()]
    parsed_changes = {}
    
    for p in pairs:
        if "=" not in p:
            return await interaction.followup.send(f"❌ 올바른 형식이 아닙니다. '=' 기호를 기준으로 입력해 주세요: `{p}`", ephemeral=True)
        k, v = p.split("=", 1)
        parsed_changes[k.strip()] = v.strip()
        
    # 2. 기존 설정 파일(.env) 분석
    current_env = read_env_file()
    
    # 3. 설정 키 존재 검증
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
        return await interaction.followup.send(f"❌ 존재하지 않는 설정 항목이 포함되어 있습니다: {invalid_str}\n대소문자나 오타를 확인해 주세요.", ephemeral=True)
        
    if not changes_to_apply:
        return await interaction.followup.send("❌ 변경할 설정 항목이 존재하지 않습니다.", ephemeral=True)
        
    # 4. 이중 승인을 위한 비교 표 임베드 빌드
    embed = discord.Embed(title="⚙️ 팰월드 설정 일괄 변경 승인 요청", color=discord.Color.blue(), timestamp=datetime.datetime.now())
    desc = "요청하신 설정값을 확인해 주세요. 아래 변경 내용을 일괄 반영하고 서버를 재시작하시겠습니까?\n\n"
    for c in changes_to_apply:
        desc += f"• **{c['key']}**: `{c['old']}` ➔ `{c['new']}`\n"
    embed.description = desc
    embed.set_footer(text="승인 버튼 클릭 시 env 파일 수정과 함께 서버 자동 재시작이 1회 시작됩니다.")
    
    view = ConfigConfirmView(changes_to_apply, interaction.user.id)
    msg = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    
    await view.wait()
    
    # 5. 승인 확인 시 일괄 반영 및 기동 처리
    if view.confirmed:
        update_dict = {c['key']: c['new'] for c in changes_to_apply}
        write_env_file(update_dict)
        
        # 봇 메모리상의 환경변수 동적 동기화
        global RCON_PWD, RCON_PORT, API_PORT
        for c in changes_to_apply:
            os.environ[c['key']] = c['new']
            if c['key'] == "ADMIN_PASSWORD":
                RCON_PWD = c['new']
            elif c['key'] == "RCON_PORT":
                RCON_PORT = int(c['new'])
            elif c['key'] == "REST_API_PORT":
                API_PORT = c['new']
                
        # 대기 안내 상태로 임베드 갱신
        embed.title = "⚙️ 설정 반영 및 서버 재기동 시작..."
        embed.color = discord.Color.orange()
        embed.description = "설정이 정상 기입되었습니다. 안전 저장을 진행하고 서버 재배치를 실행합니다."
        await msg.edit(embed=embed, view=None)
        
        # 메인 채널 공지 안내 전송 (15분 후 자동 삭제 추가 적용)
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            announce_msg = await channel.send("🔄 **[설정 변경]** 새로운 설정 사항 적용을 위해 서버가 일괄 저장 후 재배치됩니다.")
            await announce_msg.delete(delay=900)  # 15분(900초) 후 자동 삭제
            
        await send_rcon("Broadcast Server_is_restarting_to_apply_new_settings")
        await send_rcon("Save")
        await asyncio.sleep(5)
        
        # --- [추가 수정] 설정 적용 기동 직전 강제 안전 백업 무조건 실행 ---
        try:
            await run_backup()
        except Exception as b_err:
            logger.error(f"설정 적용 전 강제 백업 도중 오류 감지: {b_err}")
            
        # 설정 변경 재시작 직전 퇴장 감시 백업 예약 강제 초기화 (중복 방지 핵심 로직)
        bot.last_player_count = 0
        bot.backup_pending_on_empty = False
        bot.empty_timestamp = 0.0
        
        try:
            container = docker_client.containers.get("palworld-server")
            container.stop()
            await asyncio.sleep(5)
            
            # 수동 및 자동 통일 명령어 적용 (docker-compose up 실행, 안전한 run 사용)
            subprocess.run(
                ["docker-compose", "up", "-d", "--force-recreate"],
                cwd=BASE_PATH,
                check=True
            )
            
            data = get_status()
            data["last_restart"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
            save_status(data)
            
            # 현황판 즉시 갱신
            await refresh_dashboard()
            
            embed.title = "⚙️ 설정 일괄 변경 완료"
            embed.color = discord.Color.green()
            embed.description = "설정값 일괄 대입 및 서버 컨테이너 재배치가 성공적으로 완료되었습니다."
            await msg.edit(embed=embed, view=None)
            logger.info("관리자의 설정 일괄 변경 요청에 따른 도커 컴포즈 재구동이 완료되었습니다.")
        except Exception as err:
            logger.exception("설정 반영 도중 도커 컴포즈 재배치 기동 중 치명적 예외 발생:")
            embed.title = "❌ 설정 변경 후 재기동 오류 발생"
            embed.color = discord.Color.red()
            embed.description = f"설정 파일은 수정되었으나, 컨테이너 기동 중 에러가 발생했습니다: `{err}`"
            await msg.edit(embed=embed, view=None)

# --- 현황판 갱신 통합 함수 (도커 헬스체크 및 REST API 정보 이중 검증 연동) ---
async def refresh_dashboard():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except Exception as ch_err:
            logger.error(f"[CHANNEL ERROR] ID({CHANNEL_ID}) 채널 획득 실패: {ch_err}")
            return
            
    mem = psutil.virtual_memory()
    status, players = "OFFLINE", "0 / 0"
    color = discord.Color.red()
    current_players = 0
    
    try:
        container = docker_client.containers.get("palworld-server")
        container.reload()  # 컨테이너의 최신 상태 속성을 동기화합니다.
        
        if container.status == "running":
            # State 내부의 Health 정보를 가져옵니다.
            state = container.attrs.get("State", {})
            health = state.get("Health", {})
            health_status = health.get("Status", "none")  # starting, healthy, unhealthy, none 중 하나

            if health_status == "starting":
                status = "STARTING"
                players = "로딩 중..."
                color = discord.Color.orange()
                bot.server_version = "알 수 없음"
                bot.last_player_count = 0
            elif health_status == "unhealthy":
                status = "UNHEALTHY"
                players = "0 / 0"
                color = discord.Color.red()
                bot.server_version = "알 수 없음"
                bot.last_player_count = 0
            else:
                # healthy 상태이거나 헬스체크 미구현 시에는 내부 REST API 응답까지 최종 검증합니다.
                api_success = False
                temp_version = "알 수 없음"
                temp_players = "0 / 0"
                
                try:
                    # 1차로 서버 기본 정보 API를 확인하여 구동 완료 체크
                    info_res = requests.get(f"http://127.0.0.1:{API_PORT}/v1/api/info", auth=('admin', RCON_PWD), timeout=2)
                    if info_res.status_code == 200:
                        info_data = info_res.json()
                        temp_version = info_data.get("version", "알 수 없음")
                        
                        # 2차로 서버 성능 지표(동접자 수) 수집 시도
                        res = requests.get(f"http://127.0.0.1:{API_PORT}/v1/api/metrics", auth=('admin', RCON_PWD), timeout=2)
                        if res.status_code == 200:
                            m = res.json()
                            current_players = m.get('currentplayernum', 0)
                            temp_players = f"{current_players} / {m.get('maxplayernum', 16)}"
                            bot.last_player_count = current_players
                            api_success = True
                except Exception as api_err:
                    # 내부 REST API 웹서버가 아직 가동 전인 경우
                    api_success = False
                
                # 도커 상태가 Healthy여도 내부 API에서 버전값을 정상 획득해야만 'ONLINE'으로 최종 승격시킵니다.
                if api_success and temp_version != "알 수 없음":
                    status = "ONLINE"
                    color = discord.Color.green()
                    bot.server_version = temp_version
                    players = temp_players
                else:
                    # 포트는 열렸으나 팰월드 내부 구동 중인 과도기 단계
                    status = "STARTING"
                    players = "로딩 중..."
                    color = discord.Color.orange()
                    bot.server_version = "알 수 없음"
                    bot.last_player_count = 0
        else:
            bot.server_version = "알 수 없음"
            bot.last_player_count = 0
    except Exception as container_err:
        logger.error(f"[CONTAINER ERROR] 대시보드 데이터 수집 중 에러: {container_err}")
        bot.server_version = "알 수 없음"
        bot.last_player_count = 0

    bot.last_status = status
    data = get_status()

    # 지능형 이중 동적 자동 백업 로직
    if status == "ONLINE" and current_players >= 1:
        now_dt = datetime.datetime.now()
        required_interval = get_backup_interval(now_dt)
        last_backup_ts = data.get("last_backup_timestamp", 0.0)
        
        if time.time() - last_backup_ts >= required_interval:
            await run_backup()
            data = get_status()

    # 메모리 한계치 초과 자동 재시작
    if mem.percent > RE_LIMIT:
        current_time = time.time()
        if current_time - bot.last_restart_time > 1800:
            bot.last_restart_time = current_time
            
            await send_rcon("Broadcast Server_is_stopping_for_maintenance")
            await send_rcon("Save")
            await asyncio.sleep(5)
            try:
                container = docker_client.containers.get("palworld-server")
                container.stop()
                await asyncio.sleep(5)
                # 정지 후 재시작 시에도 컴포즈 up 명령어 적용
                subprocess.run(
                    ["docker-compose", "up", "-d", "--force-recreate"],
                    cwd=BASE_PATH,
                    check=True
                )
            except Exception as e:
                logger.error(f"메모리 초과 자동 재시작 시 시퀀스 처리 오류: {e}")
            
            data = get_status()
            data["last_restart"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
            save_status(data)

    used_gb = mem.used / (1024 ** 3)
    total_gb = mem.total / (1024 ** 3)
    ram_display = f"{used_gb:.1f}GB/{total_gb:.0f}GB ({mem.percent}%)"

    if status == "ONLINE":
        version_str = bot.server_version
        if version_str and not version_str.lower().startswith('v'):
            version_str = f"v{version_str}"
        embed_title = f"🟢 {SERVER_NAME} ({version_str})"
    elif status == "STARTING":
        embed_title = f"🟡 {SERVER_NAME} (로딩 중...)"
    elif status == "UNHEALTHY":
        embed_title = f"⚠️ {SERVER_NAME} (이상 발생)"
    else:
        embed_title = f"🔴 {SERVER_NAME}"

    # 임베드 UI 필드 구성
    embed = discord.Embed(title=embed_title, color=color, timestamp=datetime.datetime.now())
    embed.add_field(name="상태", value=f"```\n{status}\n```", inline=True)
    embed.add_field(name="접속자", value=f"```\n{players}\n```", inline=True)
    embed.add_field(name="접속 주소", value=f"```\n{SERVER_IP}:{SERVER_PORT}\n```", inline=False)
    embed.add_field(name="CPU / RAM", value=f"```\n{psutil.cpu_percent()}% / {ram_display}\n```", inline=False)
    embed.add_field(name="최근 백업", value=f"```\n{data.get('last_backup', '-')}\n```", inline=False)
    embed.add_field(name="마지막 재시작", value=f"```\n{data.get('last_restart', '-')}\n```", inline=False)
    embed.set_footer(text="1분마다 자동 갱신 (유저 변동 시 즉시 갱신)")

    # 현황판 메시지 전송 및 버튼 뷰 부착
    update_state = data.get("update_available", False)
    view = ServerControlView(update_available=update_state)
    msg_id = data.get("msg_id")
    
    if msg_id:
        try:
            msg = await channel.fetch_message(msg_id)
            await msg.edit(embed=embed, view=view)
        except Exception:
            try:
                msg = await channel.send(embed=embed, view=view)
                data["msg_id"] = msg.id
                save_status(data)
            except Exception as e:
                logger.error(f"현황판 임베드 메시지 재발송 도중 에러: {e}")
    else:
        try:
            msg = await channel.send(embed=embed, view=view)
            data["msg_id"] = msg.id
            save_status(data)
        except Exception as e:
            logger.error(f"현황판 최초 발송 중 에러 발생: {e}")

# --- 백그라운드 태스크 ---

@tasks.loop(seconds=60)
async def dashboard_task():
    try:
        await refresh_dashboard()
    except Exception as e:
        logger.error(f"dashboard_task 루프 내부 오류 감지: {e}")

@tasks.loop(seconds=5)
async def player_watcher_task():
    """5초 주기로 기동 중 혹은 접속자 변동 시 현황판을 갱신합니다. (퇴장 감시 백업 추가)"""
    if not bot.is_ready():
        return

    try:
        container = docker_client.containers.get("palworld-server")
        container.reload()
        
        state = container.attrs.get("State", {})
        health_status = state.get("Health", {}).get("Status", "none")
        
        if container.status == "running":
            # 이미 ONLINE 상태인 경우는 동접수 변동 시에만 대시보드 새로고침
            if bot.last_status == "ONLINE":
                try:
                    res = requests.get(f"http://127.0.0.1:{API_PORT}/v1/api/metrics", auth=('admin', RCON_PWD), timeout=2)
                    if res.status_code == 200:
                        m = res.json()
                        current_players = m.get('currentplayernum', 0)
                        
                        # --- [지능형 퇴장 감시 백업 알고리즘 작동] ---
                        # 1) 유저가 활발히 접속하다가 전원 퇴장하여 0명이 된 순간을 감지
                        if bot.last_player_count >= 1 and current_players == 0:
                            bot.empty_timestamp = time.time()
                            bot.backup_pending_on_empty = True
                            logger.info(f"[퇴장 감시] 마지막 유저 퇴장 감지. {EMPTY_BACKUP_DELAY}초 대기 후 안전 백업 예약.")
                            
                        # 2) 대기 시간(예: 15분) 중에 유저가 튕겼다가 다시 재접속한 경우 백업 예약 무효화
                        if current_players >= 1 and bot.backup_pending_on_empty:
                            bot.backup_pending_on_empty = False
                            bot.empty_timestamp = 0.0
                            logger.info("[퇴장 감시] 유저 재접속 확인. 예약된 안전 백업 취소됨.")
                            
                        # 3) 유저 0명 상태로 지정한 유예 기간(15분)이 경과하면 백업 수행 및 예약 해제
                        if bot.backup_pending_on_empty and current_players == 0:
                            elapsed = time.time() - bot.empty_timestamp
                            if elapsed >= EMPTY_BACKUP_DELAY:
                                bot.backup_pending_on_empty = False  # 일회성 작동을 위해 즉시 차단 플래그 강하
                                bot.empty_timestamp = 0.0
                                logger.info(f"[퇴장 감시] 유예 기간({EMPTY_BACKUP_DELAY}초)이 경과하여 최종 데이터 안전 백업을 기동합니다.")
                                await run_backup()
                                await refresh_dashboard()
                        
                        # 기존 접속 인원 수치 변동 시 실시간 갱신 루틴
                        if current_players != bot.last_player_count:
                            await refresh_dashboard()
                    else:
                        await refresh_dashboard()
                except Exception:
                    await refresh_dashboard()
            else:
                # 기동 대기 중(STARTING, OFFLINE)이지만 컨테이너는 실행 중인 단계라면,
                # 5초마다 API 조회를 시도하여 완벽가동 시 대시보드를 ONLINE으로 즉시 기상시킵니다.
                await refresh_dashboard()
        else:
            if bot.last_status != "OFFLINE":
                await refresh_dashboard()
    except Exception as e:
        if bot.last_status != "OFFLINE":
            await refresh_dashboard()

@tasks.loop(hours=8)
async def steam_update_checker_task():
    """8시간마다 스팀 API로 업데이트 유무를 판별하고 대시보드를 갱신합니다."""
    if not bot.is_ready(): return
    has_update, build_id = await check_steam_update()
    if has_update:
        await refresh_dashboard()

@tasks.loop(seconds=10)
async def scheduled_restart_task():
    if not bot.is_ready():
        return

    try:
        now_dt = datetime.datetime.now()
        if now_dt.strftime("%H:%M") == RESTART_TIME and bot.last_scheduled_restart_date != now_dt.strftime("%Y-%m-%d"):
            bot.last_scheduled_restart_date = now_dt.strftime("%Y-%m-%d")
            
            # 점검 직전 최신 스팀 패치 정보를 확인합니다.
            await check_steam_update()
            
            # 정기 점검 5분 전 공지 및 업데이트 시퀀스 시작
            await run_maintenance_sequence()
    except Exception as e:
        logger.error(f"scheduled_restart_task 루프 중 예외 발생: {e}")

@tasks.loop(hours=24)
async def scheduled_cleanup_task():
    try:
        await delete_old_backups()
    except Exception as e:
        logger.error(f"scheduled_cleanup_task 루프 도중 예외 발생: {e}")

@bot.event
async def on_ready():
    logger.info(f"봇 로그인 완료: {bot.user.name}")
    try:
        await bot.fetch_channel(CHANNEL_ID)
        await bot.fetch_channel(LOG_CHANNEL_ID)
    except Exception as e:
        logger.warning(f"초기 채널 캐싱 오류 (채널이 활성화 전이거나 도달 불가능, 무시 가능): {e}")

    # 최초 업데이트 감지 한 번 수행
    await check_steam_update()

    if not dashboard_task.is_running(): dashboard_task.start()
    if not player_watcher_task.is_running(): player_watcher_task.start()
    if not steam_update_checker_task.is_running(): steam_update_checker_task.start()
    if not scheduled_restart_task.is_running(): scheduled_restart_task.start()
    if not scheduled_cleanup_task.is_running(): scheduled_cleanup_task.start()

bot.run(TOKEN)