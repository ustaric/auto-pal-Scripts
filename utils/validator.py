import os
from utils.logger import logger

def validate_env_variables():
    logger.info("서버 시작 전 환경변수 안전 무결성 검증을 실시합니다...")
    errors = []
    
    token = os.getenv("DISCORD_TOKEN")
    channel_id = os.getenv("CHANNEL_ID")
    base_path = os.getenv("BASE_PATH")
    rcon_port = os.getenv("RCON_PORT")
    api_port = os.getenv("REST_API_PORT")
    re_limit = os.getenv("MEMORY_RESTART_THRESHOLD", "80")
    retention_days = os.getenv("BACKUP_RETENTION_DAYS", "2")

    def check_port(val, name):
        try:
            p = int(val)
            if not (1 <= p <= 65535):
                errors.append(f"{name} 은(는) 1에서 65535 사이의 유효한 포트 범위여야 합니다. (입력값: {val})")
        except (ValueError, TypeError):
            errors.append(f"{name} 은(는) 숫자 정수 형식이어야 합니다. (입력값: {val})")

    if not token:
        errors.append("DISCORD_TOKEN 이 env 설정 파일에 누락되었습니다.")
    if not channel_id:
        errors.append("CHANNEL_ID 가 env 설정 파일에 누락되었습니다.")
    if not base_path:
        errors.append("BASE_PATH 가 env 설정 파일에 누락되었습니다.")
    elif not os.path.exists(base_path):
        errors.append(f"BASE_PATH 로 지정된 경로가 실제 서버 파일 시스템에 존재하지 않습니다: {base_path}")
        
    check_port(rcon_port, "RCON_PORT")
    check_port(api_port, "REST_API_PORT")
    
    try:
        int(re_limit)
    except (ValueError, TypeError):
        errors.append(f"MEMORY_RESTART_THRESHOLD 는 숫자 형식이어야 합니다. (입력값: {re_limit})")
        
    try:
        int(retention_days)
    except (ValueError, TypeError):
        errors.append(f"BACKUP_RETENTION_DAYS 는 숫자 형식이어야 합니다. (입력값: {retention_days})")

    if errors:
        for err in errors:
            logger.error(f"[환경변수 무결성 검증 실패] {err}")
        logger.critical("설정값에 중대한 에러가 발견되어 디스코드 봇의 기동을 전면 안전 중단합니다.")
        raise ValueError("환경변수 검증 실패. env 및 .env 파일의 비정상적인 값을 올바르게 조율해 주세요.")
    
    logger.info("모든 환경변수 검증이 안전하게 완료되었습니다. 정상 기동으로 진입합니다.")