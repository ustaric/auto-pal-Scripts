# services/config_service.py
import os
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

    def read_env_file(self):
        env_path = os.path.join(self.base_path, ".env") if self.base_path else ".env"
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