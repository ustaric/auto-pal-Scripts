import docker
import asyncio
from mcrcon import MCRcon
from utils.logger import logger

class DockerService:
    def __init__(self, config_service):
        self.config = config_service
        self.docker_client = docker.from_env()

    async def send_rcon(self, command):
        try:
            # RCON 연결 차단 방지를 위해 동기 소켓 작업을 별도 스레드풀에서 안전 기동시킵니다.
            def _run():
                with MCRcon("127.0.0.1", self.config.rcon_pwd, port=self.config.rcon_port) as mcr:
                    return mcr.command(command)
            return await asyncio.to_thread(_run)
        except Exception as e: 
            logger.error(f"MCRcon 연결 혹은 명령 실행 실패 (커맨드: {command}): {e}")
            return "RCON_ERROR"

    def get_container(self, name="palworld-server"):
        try:
            container = self.docker_client.containers.get(name)
            container.reload()
            return container
        except Exception as e:
            logger.error(f"도커 컨테이너 '{name}' 정보 획득 실패: {e}")
            return None