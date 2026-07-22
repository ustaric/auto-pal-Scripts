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
        """서버 데이터를 안전하게 저장하고 아카이빙 압축 처리한 뒤 결과를 로그 채널에 송신합니다."""
        now = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        target = f"{self.config.backup_path}/{now}.tar.gz"
        
        try:
            # [안정화 개선] 압축 전에 RCON 'Save' 명령을 내릴 수 있는 상태(서버 ONLINE)인지 판별하여
            # 활성 세이브 데이터를 완벽하게 디스크로 강제 플러시(Force Flush)합니다.
            if bot.last_status == "ONLINE":
                try:
                    logger.info("안전한 백업 생성을 위해 RCON Save 명령어를 전송합니다...")
                    await bot.docker_service.send_rcon("Save")
                    await asyncio.sleep(3)  # 디스크 쓰기 작업 완료를 위해 3초 대기
                except Exception as rcon_err:
                    logger.warning(f"RCON 저장 명령어 전송 실패 (백업 작업을 계속 강행합니다): {rcon_err}")

            # [수정] 도커 내부 중복 백업 명령을 완전히 제거하고, 
            # 호스트 단의 파이썬 tar.gz 아카이빙 작업만 실행하여 오류를 방지합니다.

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
        """기한이 지난 백업을 정리하되, 가장 최신 백업 시점을 기준으로 이전 24시간 동안의 모든 백업 파일은 안전 보존합니다."""
        now = time.time()
        count = 0
        deleted_files = []
        try:
            # 1. 백업 폴더 내의 모든 백업 파일 조회 (.tar.gz 형식만)
            backup_files = []
            for f in os.listdir(self.config.backup_path):
                f_path = os.path.join(self.config.backup_path, f)
                if os.path.isfile(f_path) and f.endswith(".tar.gz"):
                    backup_files.append((f, os.stat(f_path).st_mtime))
            
            if not backup_files:
                logger.info("정리할 백업 파일이 존재하지 않습니다.")
                return

            # 2. 보관된 파일 중 가장 최신에 생성된 백업 시각(T_max)을 구합니다.
            latest_mtime = max(backup_files, key=lambda x: x[1])[1]
            
            # T_max 기준으로 이전 24시간(86400초) 이내의 파일들은 보호 영역으로 지정합니다.
            protection_threshold = latest_mtime - 86400

            # 3. 파일 삭제 여부 체크 및 만료 정리
            for f, mtime in backup_files:
                # 마지막 활성 구간(T_max)으로부터 24시간 이내에 생성된 백업 파일은 무조건 보존 (생략)
                if mtime >= protection_threshold:
                    continue
                
                # 보호 대상 이외의 구버전 파일 중 설정된 보존 기한(retention_days)을 초과한 경우에만 삭제
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
                    
                    # 기준 시점을 알기 쉽도록 가독성 높은 일시 스트링으로 변환
                    session_time_str = datetime.datetime.fromtimestamp(latest_mtime).strftime('%Y-%m-%d %H:%M')
                    await log_channel.send(
                        f"🧹 **[백업 정리 완료]** 보존 기간({self.config.retention_days}일)이 지난 오래된 백업 파일 {count}개를 삭제했습니다.\n"
                        f"⚠️ **안내**: 가장 최근 백업({session_time_str}) 시점으로부터 이전 24시간 동안 생성된 모든 백업본은 보존 정책에 따라 삭제 대상에서 자동 제외되었습니다.\n"
                        f"🗑️ **삭제 목록:**\n{files_list}"
                    )
                logger.info(f"보존 기한이 초과된 구버전 백업 {count}개가 자동 디스크 정리되었습니다.")
        except Exception as e:
            logger.exception(f"오래된 백업 디스크 정리 중 실패 오류: {e}")