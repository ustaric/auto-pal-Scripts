import os
import time
import tarfile
import datetime
import subprocess
import asyncio
from utils.logger import logger

class BackupService:
    def __init__(self, config_service, status_service):
        self.config = config_service
        self.status_service = status_service

    def get_backup_interval(self, now_dt):
        weekday = now_dt.weekday()
        hour = now_dt.hour
        if hour < 2:
            weekday = (weekday - 1) % 7
            hour += 24
        if weekday <= 4:
            if 19 <= hour < 26: 
                return 30 * 60
        else:
            if 9 <= hour < 26: 
                return 30 * 60
        return 60 * 60

    async def run_backup(self, bot):
        """서버 데이터를 안전하게 저장하고 아카이빙 압축 처리한 뒤 결과를 구글 드라이브로 전송하고 로그를 송신합니다."""
        now = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        target = f"{self.config.backup_path}/{now}.tar.gz"
        
        try:
            # 1. RCON 'Save' 명령 전송 및 플러시 대기
            if bot.last_status == "ONLINE":
                try:
                    logger.info("안전한 백업 생성을 위해 RCON Save 명령어를 전송합니다...")
                    await bot.docker_service.send_rcon("Save")
                    await asyncio.sleep(3)  # 디스크 쓰기 완료 대기
                except Exception as rcon_err:
                    logger.warning(f"RCON 저장 명령어 전송 실패 (백업 작업을 계속 강행합니다): {rcon_err}")

            # 2. 로컬 압축 파일(.tar.gz) 비동기 생성
            def _create_tar():
                with tarfile.open(target, "w:gz") as tar:
                    tar.add(self.config.save_data_path, arcname="Saved")
            await asyncio.to_thread(_create_tar)
            
            logger.info(f"로컬 백업 파일 생성 완료: {target}")

            # 3. Rclone을 사용한 구글 드라이브 'backup' 폴더 업로드 (비동기 서브프로세스 실행)
            # 봇의 메인 루프가 블로킹되지 않도록 create_subprocess_exec를 사용합니다.
            logger.info("Rclone 구글 드라이브 업로드를 시작합니다...")
            rclone_proc = await asyncio.create_subprocess_exec(
                'rclone', 'copy', target, 'gdrive:backup',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # 업로드 프로세스가 끝날 때까지 비동기 대기
            stdout, stderr = await rclone_proc.communicate()
            
            if rclone_proc.returncode == 0:
                logger.info("구글 드라이브 업로드에 성공했습니다.")
                gdrive_status = "구글 드라이브 전송 완료"
            else:
                error_msg = stderr.decode().strip()
                logger.error(f"Rclone 전송 실패: {error_msg}")
                gdrive_status = f"구글 드라이브 전송 실패 ({error_msg})"

            # 4. 상태 데이터 업데이트
            data = self.status_service.get_status()
            data["last_backup"] = f"{now}.tar.gz"
            data["last_backup_timestamp"] = time.time()
            self.status_service.save_status(data)
            
            # 5. 디스코드 채널 알림 발송
            log_channel = bot.get_channel(self.config.log_channel_id)
            if log_channel:
                await log_channel.send(
                    f"📂 **[백업 결과 알림]**\n"
                    f"• 파일명: `{now}.tar.gz`\n"
                    f"• 로컬 저장: `성공`\n"
                    f"• 구글 드라이브 전송: `{gdrive_status}`"
                )
            return True, f"{now}.tar.gz"
            
        except Exception as e:
            logger.exception("백업 데이터 처리 중 치명적인 예외가 발생했습니다:")
            log_channel = bot.get_channel(self.config.log_channel_id)
            if log_channel:
                await log_channel.send(f"❌ **[백업 실패]** 백업 진행 중 오류 발생: `{e}`")
            return False, None

    async def delete_old_backups(self, bot):
        """기한이 지난 백업을 정리하되, 가장 최신 백업 시점을 기준으로 이전 24시간 동안의 모든 백업 파일은 안전 보존합니다."""
        now = time.time()
        count = 0
        deleted_files = []
        try:
            backup_files = []
            for f in os.listdir(self.config.backup_path):
                f_path = os.path.join(self.config.backup_path, f)
                if os.path.isfile(f_path) and f.endswith(".tar.gz"):
                    backup_files.append((f, os.stat(f_path).st_mtime))
            
            if not backup_files:
                logger.info("정리할 백업 파일이 존재하지 않습니다.")
                return

            latest_mtime = max(backup_files, key=lambda x: x[1])[1]
            protection_threshold = latest_mtime - 86400

            for f, mtime in backup_files:
                if mtime >= protection_threshold:
                    continue
                
                if mtime < now - (self.config.retention_days * 86400):
                    f_path = os.path.join(self.config.backup_path, f)
                    os.remove(f_path)
                    count += 1
                    deleted_files.append(f)

            if count > 0:
                data = self.status_service.get_status()
                data["last_cleanup"] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
                self.status_service.save_status(data)
                
                log_channel = bot.get_channel(self.config.log_channel_id)
                if log_channel: 
                    files_list = "\n".join([f"- `{x}`" for x in deleted_files])
                    session_time_str = datetime.datetime.fromtimestamp(latest_mtime).strftime('%Y-%m-%d %H:%M')
                    await log_channel.send(
                        f"🧹 **[백업 정리 완료]** 보존 기간({self.config.retention_days}일)이 지난 오래된 백업 파일 {count}개를 삭제했습니다.\n"
                        f"⚠️ **안내**: 가장 최근 백업({session_time_str}) 시점으로부터 이전 24시간 동안 생성된 모든 백업본은 보존 정책에 따라 삭제 대상에서 자동 제외되었습니다.\n"
                        f"🗑️ **삭제 목록:**\n{files_list}"
                    )
                logger.info(f"보존 기한이 초과된 구버전 백업 {count}개가 자동 디스크 정리되었습니다.")
        except Exception as e:
            logger.exception(f"오래된 백업 디스크 정리 중 실패 오류: {e}")