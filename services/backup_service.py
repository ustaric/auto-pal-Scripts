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
        """서버 데이터를 안전하게 아카이빙 압축 처리하고 결과를 로그 채널에 송신합니다."""
        now = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        target = f"{self.config.backup_path}/{now}.tar.gz"
        
        try:
            # docker exec 작업 비동기 격리 실행
            def _exec_backup():
                subprocess.run(
                    ["docker", "exec", "palworld-server", "backup"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
            await asyncio.to_thread(_exec_backup)

            # 압축 아카이빙 비동기 격리 실행 (I/O 루프 병목 차단)
            def _create_tar():
                with tarfile.open(target, "w:gz") as tar:
                    tar.add(self.config.save_data_path, arcname="Saved")
            await asyncio.to_thread(_create_tar)
            
            data = self.status_service.get_status()
            data["last_backup"] = f"{now}.tar.gz"
            data["last_backup_timestamp"] = time.time()
            self.status_service.save_status(data)
            
            log_channel = bot.get_channel(self.config.log_channel_id)
            if log_channel:
                await log_channel.send(f"📂 **[백업 성공]** 세이브 파일 백업 완료.\n📄 파일명: `{now}.tar.gz`")
            logger.info(f"정기 백업 저장이 완수되었습니다: {now}.tar.gz")
            return True, f"{now}.tar.gz"
            
        except Exception as e:
            logger.exception("백업 데이터 파일 압축 및 생성 중 치명적인 예외가 보고되었습니다:")
            log_channel = bot.get_channel(self.config.log_channel_id)
            if log_channel:
                await log_channel.send(f"❌ **[백업 실패]** 백업 진행 중 오류 발생: `{e}`")
            return False, None

    async def delete_old_backups(self, bot):
        now = time.time()
        count = 0
        deleted_files = []
        try:
            for f in os.listdir(self.config.backup_path):
                f_path = os.path.join(self.config.backup_path, f)
                if os.stat(f_path).st_mtime < now - (self.config.retention_days * 86400):
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
                    await log_channel.send(
                        f"🧹 **[백업 정리 완료]** 보존 기간({self.config.retention_days}일)이 지난 오래된 백업 파일 {count}개를 삭제했습니다.\n"
                        f"🗑️ **삭제 목록:**\n{files_list}"
                    )
                logger.info(f"보존 기한이 초과된 구버전 백업 {count}개가 자동 디스크 정리되었습니다.")
        except Exception as e:
            logger.exception(f"오래된 백업 디스크 정리 중 실패 오류: {e}")