import logging
import os
from logging.handlers import RotatingFileHandler

# 로거 생성
logger = logging.getLogger("palworld_bot")
logger.setLevel(logging.INFO)

# 로그 포맷 규칙 설정
formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# 콘솔 출력용 핸들러 등록
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# 파일 자동 순환 저장용 핸들러 등록 (최대 5MB, 최대 3개 보존)
file_handler = RotatingFileHandler("palworld_bot.log", maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)