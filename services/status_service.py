import os
import json
from utils.logger import logger

class StatusService:
    def __init__(self, base_path):
        self.base_path = base_path
        self.status_file_path = os.path.join(self.base_path, "status.json")

    def get_status(self):
        try:
            if os.path.exists(self.status_file_path):
                with open(self.status_file_path, "r") as f: 
                    return json.load(f)
        except Exception as e: 
            logger.warning(f"status.json 파일을 읽지 못해 기본 객체를 로드합니다: {e}")
            
        return {
            "last_backup": "-", 
            "last_cleanup": "-", 
            "last_restart": "-", 
            "msg_id": None,
            "last_backup_timestamp": 0.0,
            "known_steam_build": "0",
            "update_available": False
        }

    def save_status(self, data):
        try:
            with open(self.status_file_path, "w") as f: 
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.exception(f"status.json 저장 도중 예외가 기록되었습니다: {e}")