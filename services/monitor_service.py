import requests
import asyncio
import psutil
from utils.logger import logger

class MonitorService:
    def __init__(self, config_service, status_service):
        self.config = config_service
        self.status_service = status_service

    async def check_steam_update(self):
        """스팀 API를 통해 최신 빌드를 대조 판별합니다."""
        try:
            def _fetch():
                return requests.get("https://api.steamcmd.net/v1/info/2394010", timeout=8)
            res = await asyncio.to_thread(_fetch)
            
            if res.status_code == 200:
                data = res.json()
                latest_build = data.get("data", {}).get("2394010", {}).get("depots", {}).get("branches", {}).get("public", {}).get("buildid")
                if latest_build:
                    status_data = self.status_service.get_status()
                    old_build = status_data.get("known_steam_build", "0")
                    
                    if old_build != "0" and str(old_build) != str(latest_build):
                        status_data["update_available"] = True
                    status_data["latest_checked_build"] = str(latest_build)
                    self.status_service.save_status(status_data)
                    return status_data["update_available"], latest_build
        except Exception as e:
            logger.error(f"스팀 업데이트 정보 대조 통신 중 에러 감지: {e}")
        return False, None

    async def get_server_metrics(self):
        """팰월드 REST API 웹서버 기반 성능 수치 및 유저 수, 버전을 수집합니다."""
        api_port = self.config.api_port
        rcon_pwd = self.config.rcon_pwd
        
        try:
            def _request_info():
                return requests.get(f"http://127.0.0.1:{api_port}/v1/api/info", auth=('admin', rcon_pwd), timeout=2)
            info_res = await asyncio.to_thread(_request_info)
            
            if info_res.status_code == 200:
                info_data = info_res.json()
                version = info_data.get("version", "알 수 없음")
                
                def _request_metrics():
                    return requests.get(f"http://127.0.0.1:{api_port}/v1/api/metrics", auth=('admin', rcon_pwd), timeout=2)
                metrics_res = await asyncio.to_thread(_request_metrics)
                
                if metrics_res.status_code == 200:
                    m_data = metrics_res.json()
                    current_players = m_data.get('currentplayernum', 0)
                    max_players = m_data.get('maxplayernum', 16)
                    return True, version, current_players, max_players
                    
        except Exception:
            pass
            
        return False, "알 수 없음", 0, 0

    def get_system_resources(self):
        mem = psutil.virtual_memory()
        cpu_percent = psutil.cpu_percent()
        used_gb = mem.used / (1024 ** 3)
        total_gb = mem.total / (1024 ** 3)
        ram_display = f"{used_gb:.1f}GB/{total_gb:.0f}GB ({mem.percent}%)"
        return cpu_percent, ram_display, mem.percent