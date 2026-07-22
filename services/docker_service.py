import docker
import asyncio
from utils.logger import logger

class DockerService:
    def __init__(self, config_service):
        self.config = config_service
        self.docker_client = docker.from_env()

    async def send_rcon(self, command):
        try:
            # -it 옵션은 백그라운드 서비스(TTY가 없는 환경)에서 실행 시 에러를 유발하므로 뺍니다.
            # Rclone과 마찬가지로 create_subprocess_exec를 사용하여 봇의 메인 스레드 블로킹을 방지합니다.
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", "palworld-server",
                "rcon-cli",
                "--address", f"127.0.0.1:{self.config.rcon_port}",
                "--password", self.config.rcon_pwd,
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await proc.communicate()
            
            if proc.returncode == 0:
                response = stdout.decode("utf-8", errors="ignore").strip()
                logger.info(f"RCON 명령어 성공 (커맨드: {command}): {response}")
                return response
            else:
                error_msg = stderr.decode("utf-8", errors="ignore").strip()
                logger.error(f"RCON 명령어 실행 실패 (커맨드: {command}): {error_msg}")
                return "RCON_ERROR"
                
        except Exception as e:
            logger.error(f"RCON 서브프로세스 기동 중 예외 발생 (커맨드: {command}): {e}")
            return "RCON_ERROR"

    def get_container(self, name="palworld-server"):
        try:
            container = self.docker_client.containers.get(name)
            container.reload()
            return container
        except Exception as e:
            logger.error(f"도커 컨테이너 '{name}' 정보 획득 실패: {e}")
            return None