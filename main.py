import discord
from discord import app_commands
from discord.ext import commands, tasks
import os, json, psutil, docker, requests, tarfile, datetime, asyncio, time
from dotenv import load_dotenv
from mcrcon import MCRcon

# 설정 로드
load_dotenv(dotenv_path="env")
TOKEN = os.getenv("DISCORD_TOKEN")

# 여러 개의 관리자 ID를 쉼표로 분리하여 리스트로 변환합니다.
admin_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in admin_raw.split(",") if x.strip()]

CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
# 지정한 로그 전용 채널 ID를 가져옵니다. 없을 시 기본 채널 ID를 할당합니다.
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", 1255145931255582770))

SERVER_NAME = os.getenv("SERVER_NAME")
SERVER_IP = os.getenv("SERVER_IP")
SERVER_PORT = os.getenv("SERVER_PORT")
RCON_PWD = os.getenv("ADMIN_PASSWORD")
RCON_PORT = int(os.getenv("RCON_PORT"))
API_PORT = os.getenv("REST_API_PORT")
SAVE_DATA_PATH = os.getenv("SAVE_DATA_PATH")
BACKUP_PATH = os.getenv("BACKUP_PATH")
BASE_PATH = os.getenv("BASE_PATH")
RE_LIMIT = int(os.getenv("MEMORY_RESTART_THRESHOLD", 80))
RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", 2))
RESTART_TIME = os.getenv("RESTART_TIME", "04:00")

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)
        self.server_version = "알 수 없음"
        # 유저 수 변화 감지 및 무한 재시작 방지용 캐시 변수
        self.last_player_count = -1
        self.last_status = "UNKNOWN"
        self.last_restart_time = 0.0          # 자동 재시작 쿨다운용 (타임스탬프)
        self.last_scheduled_restart_date = ""  # 하루 한 번 정기 점검 보장용 (YYYY-MM-DD)

    async def setup_hook(self):
        await self.tree.sync()
        print("Slash commands synced.")

bot = MyBot()
docker_client = docker.from_env()

# --- 유틸리티 ---
def get_status():
    try:
        with open(f"{BASE_PATH}/status.json", "r") as f: return json.load(f)
    except: 
        return {
            "last_backup": "-", 
            "last_cleanup": "-", 
            "last_restart": "-", 
            "msg_id": None,
            "last_backup_timestamp": 0.0
        }

def save_status(data):
    with open(f"{BASE_PATH}/status.json", "w") as f: json.dump(data, f)

async def send_rcon(command):
    try:
        with MCRcon("127.0.0.1", RCON_PWD, port=RCON_PORT) as mcr:
            return mcr.command(command)
    except: return "RCON_ERROR"

async def run_backup():
    now = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    target = f"{BACKUP_PATH}/{now}.tar.gz"
    try:
        os.system("docker exec palworld-server backup")
        with tarfile.open(target, "w:gz") as tar:
            tar.add(SAVE_DATA_PATH, arcname="Saved")
        data = get_status()
        data["last_backup"] = f"{now}.tar.gz"
        data["last_backup_timestamp"] = time.time()  # 백업 시점의 타임스탬프 기록
        save_status(data)
        
        # 로그 채널로 알림 전송
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"📂 **[백업 성공]** 세이브 파일이 성공적으로 압축 및 백업되었습니다.\n📄 파일명: `{now}.tar.gz`")
            
        return True, f"{now}.tar.gz"
    except Exception as e:
        print(f"Backup Error: {e}")
        
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"❌ **[백업 실패]** 백업 생성 중 오류가 발생했습니다.\n⚠️ 오류 내용: `{e}`")
            
        return False, None

# 오래된 백업 삭제 함수
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
            print(f"Cleanup: Deleted {count} old backup files.")
            
            # 로그 채널로 알림 전송
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                files_list = "\n".join([f"- `{x}`" for x in deleted_files])
                await log_channel.send(
                    f"🧹 **[백업 정리 완료]** 보존 기간({RETENTION_DAYS}일)이 지난 오래된 백업 파일 {count}개를 삭제했습니다.\n"
                    f"🗑️ **삭제 목록:**\n{files_list}"
                )
    except Exception as e:
        print(f"Cleanup Error: {e}")
        
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            await log_channel.send(f"❌ **[백업 정리 실패]** 오래된 백업을 정리하던 도중 예외가 발생했습니다.\n⚠️ 오류 내용: `{e}`")

# --- 이중 동적 백업 주기를 결정하는 도우미 함수 ---
def get_backup_interval(now_dt):
    weekday = now_dt.weekday()  # 0=월, 1=화, ..., 4=금, 5=토, 6=일
    hour = now_dt.hour

    if hour < 2:
        weekday = (weekday - 1) % 7
        hour += 24

    # 1. 평일 피크 타임 (월~금) 오후 7시(19) ~ 다음날 새벽 2시(26)
    if weekday <= 4:
        if 19 <= hour < 26:
            return 30 * 60
            
    # 2. 주말 피크 타임 (토~일) 오전 9시(9) ~ 다음날 새벽 2시(26)
    else:
        if 9 <= hour < 26:
            return 30 * 60

    # 3. 그 외의 모든 시간 (비피크 타임 일반 모드)
    return 60 * 60

# --- 서버 제어 코어 로직 ---
async def safe_stop_sequence():
    await send_rcon("Broadcast Server_is_stopping_for_maintenance")
    await send_rcon("Save")
    await asyncio.sleep(5)
    container = docker_client.containers.get("palworld-server")
    container.stop()

# --- 슬래시 명령어 ---
@bot.tree.command(name="start", description="팰월드 서버를 시작합니다.")
async def start(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    await interaction.response.defer()
    try:
        container = docker_client.containers.get("palworld-server")
        if container.status == "running":
            await interaction.followup.send("ℹ️ 서버가 이미 실행 중입니다.")
        else:
            container.start()
            await interaction.followup.send("✅ 서버 가동을 시작했습니다.")
    except Exception as e:
        await interaction.followup.send(f"❌ 오류 발생: {e}")

@bot.tree.command(name="stop", description="팰월드 서버를 안전하게 정지합니다.")
async def stop(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    await interaction.response.send_message("🛑 서버를 안전하게 저장 후 정지합니다. (약 10초 소요)")
    await safe_stop_sequence()
    await interaction.followup.send("✅ 서버가 정지되었습니다.")

@bot.tree.command(name="restart", description="팰월드 서버를 안전하게 재시작합니다.")
async def restart(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    await interaction.response.send_message("🔄 서버 재시작 시퀀스 가동 (공지 -> 저장 -> 정지 -> 시작)")
    await safe_stop_sequence()
    await asyncio.sleep(5)
    container = docker_client.containers.get("palworld-server")
    container.start()
    bot.last_restart_time = time.time()  # 수동 재시작 시점 기록
    data = get_status(); data["last_restart"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M'); save_status(data)
    await interaction.followup.send("✅ 서버가 다시 시작되었습니다.")

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
    except Exception as e:
        await interaction.followup.send(f"❌ 오류 발생: {e}", ephemeral=True)    

# 관리자용 수동 백업 명령어
@bot.tree.command(name="backup", description="팰월드 세이브 데이터를 수동으로 즉시 백업합니다.")
async def manual_backup(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_IDS:
        return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        success, filename = await run_backup()
        if success:
            await interaction.followup.send(f"✅ 수동 백업을 성공적으로 완료했습니다.\n📂 백업 파일: `{filename}`", ephemeral=True)
        else:
            await interaction.followup.send("❌ 백업 실패: 파일 압축 도중 오류가 발생했습니다.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ 오류 발생: {e}", ephemeral=True)


# --- 현황판 갱신 통합 함수 ---
async def refresh_dashboard():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        try:
            channel = await bot.fetch_channel(CHANNEL_ID)
        except Exception as ch_err:
            print(f"[CHANNEL ERROR] ID({CHANNEL_ID}) 채널 획득 실패: {ch_err}")
            return
            
    mem = psutil.virtual_memory()
    status, players = "OFFLINE", "0 / 0"
    color = discord.Color.red()
    current_players = 0
    
    try:
        container = docker_client.containers.get("palworld-server")
        if container.status == "running":
            status = "ONLINE"
            color = discord.Color.green()
            try:
                # API 호출로 유저 수 및 메트릭 획득
                res = requests.get(f"http://127.0.0.1:{API_PORT}/v1/api/metrics", auth=('admin', RCON_PWD), timeout=2)
                if res.status_code == 200:
                    m = res.json()
                    current_players = m.get('currentplayernum', 0)
                    players = f"{current_players} / {m.get('maxplayernum', 16)}"
                    # 변경 감지를 위한 캐시 업데이트
                    bot.last_player_count = current_players
                else:
                    print(f"[API ERROR] HTTP Status Code: {res.status_code}")
                
                if bot.server_version == "알 수 없음" or not bot.server_version:
                    info_res = requests.get(f"http://127.0.0.1:{API_PORT}/v1/api/info", auth=('admin', RCON_PWD), timeout=2)
                    if info_res.status_code == 200:
                        info_data = info_res.json()
                        bot.server_version = info_data.get("version", "알 수 없음")
            except Exception as api_err:
                print(f"[API CONNECTION ERROR] {api_err}")
        else:
            bot.server_version = "알 수 없음"
            bot.last_player_count = 0
    except Exception as container_err:
        print(f"[CONTAINER ERROR] {container_err}")
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
            print(f"[AUTO BACKUP] 유저 활동 감지 및 시간 도달로 자동 백업을 구동합니다. (주기: {required_interval // 60}분)")
            await run_backup()
            data = get_status()

    # 메모리 한계치 초과 자동 재시작 (무한 루프 방지용 쿨다운 30분 적용)
    if mem.percent > RE_LIMIT:
        current_time = time.time()
        if current_time - bot.last_restart_time > 1800:
            print(f"[AUTO RESTART] 메모리 점유율({mem.percent}%)이 기준치({RE_LIMIT}%)를 초과하여 자동 재시작을 진행합니다.")
            bot.last_restart_time = current_time # 시점 즉시 기록으로 연쇄 방지
            
            await safe_stop_sequence()
            await asyncio.sleep(5)
            docker_client.containers.get("palworld-server").start()
            
            data = get_status()
            data["last_restart"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
            save_status(data)
        else:
            print("[AUTO RESTART] 호스트 메모리가 높으나 최근 30분 이내에 재시작한 이력이 있어 생략합니다.")

    used_gb = mem.used / (1024 ** 3)
    total_gb = mem.total / (1024 ** 3)
    ram_display = f"{used_gb:.1f}GB/{total_gb:.0f}GB ({mem.percent}%)"

    if status == "ONLINE":
        version_str = bot.server_version
        if version_str and not version_str.lower().startswith('v'):
            version_str = f"v{version_str}"
        embed_title = f"🟢 {SERVER_NAME} ({version_str})"
    else:
        embed_title = f"🔴 {SERVER_NAME}"

    embed = discord.Embed(title=embed_title, color=color, timestamp=datetime.datetime.now())
    embed.add_field(name="상태", value=f"```\n{status}\n```", inline=True)
    embed.add_field(name="접속자", value=f"```\n{players}\n```", inline=True)
    embed.add_field(name="접속 주소", value=f"```\n{SERVER_IP}:{SERVER_PORT}\n```", inline=False)
    embed.add_field(name="CPU / RAM", value=f"```\n{psutil.cpu_percent()}% / {ram_display}\n```", inline=False)
    embed.add_field(name="최근 백업", value=f"```\n{data.get('last_backup', '-')}\n```", inline=False)
    embed.add_field(name="마지막 재시작", value=f"```\n{data.get('last_restart', '-')}\n```", inline=False)
    embed.set_footer(text="1분마다 자동 갱신 (유저 변동 시 즉시 갱신)")

    if data.get("msg_id"):
        try:
            msg = await channel.fetch_message(data["msg_id"])
            await msg.edit(embed=embed)
        except:
            msg = await channel.send(embed=embed)
            data["msg_id"] = msg.id
            save_status(data)
    else:
        msg = await channel.send(embed=embed)
        data["msg_id"] = msg.id
        save_status(data)


# --- 백그라운드 태스크 ---

# 1. 메인 대시보드 주기적 업데이트 (기본 정보 수집용, 1분 주기)
@tasks.loop(seconds=60)
async def dashboard_task():
    await refresh_dashboard()

# 2. 실시간 접속자 감시 태스크 (8초마다 변경 사항 체크)
@tasks.loop(seconds=8)
async def player_watcher_task():
    if not bot.is_ready():
        return

    try:
        container = docker_client.containers.get("palworld-server")
        if container.status == "running":
            res = requests.get(f"http://127.0.0.1:{API_PORT}/v1/api/metrics", auth=('admin', RCON_PWD), timeout=2)
            if res.status_code == 200:
                m = res.json()
                current_players = m.get('currentplayernum', 0)
                
                if current_players != bot.last_player_count or bot.last_status != "ONLINE":
                    print(f"[REALTIME DETECTED] 접속자 수 변경 감지: {bot.last_player_count} -> {current_players}. 현황판을 갱신합니다.")
                    await refresh_dashboard()
            else:
                if bot.last_status != "OFFLINE":
                    await refresh_dashboard()
        else:
            if bot.last_status != "OFFLINE":
                await refresh_dashboard()
    except Exception as e:
        if bot.last_status != "OFFLINE":
            await refresh_dashboard()


# --- 정기 점검 태스크 (하루 한 번 안전 점검) ---
@tasks.loop(seconds=10) # 10초마다 빠르게 체크하여 정각에 즉시 대응
async def scheduled_restart_task():
    if not bot.is_ready():
        return

    now_dt = datetime.datetime.now()
    now_time = now_dt.strftime("%H:%M")
    today_str = now_dt.strftime("%Y-%m-%d")
    
    # 시간 일치 확인 및 금일 중복 실행 여부 대조
    if now_time == RESTART_TIME and bot.last_scheduled_restart_date != today_str:
        bot.last_scheduled_restart_date = today_str # 즉시 실행 표시 기록
        
        channel = bot.get_channel(CHANNEL_ID)
        if channel:
            await channel.send(f"⏰ 정기 점검 시간({RESTART_TIME})입니다. 백업 후 서버 재시작을 진행합니다.")
        
        await safe_stop_sequence()
        await asyncio.sleep(5)
        
        await run_backup()
        
        container = docker_client.containers.get("palworld-server")
        container.start()
        
        bot.last_restart_time = time.time() # 시점 갱신으로 불필요한 자동 재시작 대기 설정
        
        data = get_status()
        data["last_restart"] = now_dt.strftime('%Y-%m-%d %H:%M')
        save_status(data)
        
        if channel: await channel.send("✅ 정기 점검 및 재시작이 완료되었습니다.")

# --- 백업 삭제 태스크 (하루 한 번 실행) ---
@tasks.loop(hours=24)
async def scheduled_cleanup_task():
    await delete_old_backups()

@bot.event
async def on_ready():
    print(f"Logged in: {bot.user.name}")
    
    # 디스코드 내부 캐시에 채널들을 등록하기 위해 fetch 및 준비 작업 수행
    try:
        await bot.fetch_channel(CHANNEL_ID)
        await bot.fetch_channel(LOG_CHANNEL_ID)
    except Exception as e:
        print(f"[WARNING] 초기 채널 캐싱 오류 (무시 가능): {e}")

    if not dashboard_task.is_running(): dashboard_task.start()
    if not player_watcher_task.is_running(): player_watcher_task.start()
    if not scheduled_restart_task.is_running(): scheduled_restart_task.start()
    if not scheduled_cleanup_task.is_running(): scheduled_cleanup_task.start()

bot.run(TOKEN)