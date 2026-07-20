#!/bin/bash

# 에러 발생 시 즉시 중단
set -e

# 실제 로그인한 사용자 감지 (sudo로 실행해도 실제 계정인 rsa-key-20260719 감지)
ACTUAL_USER=${SUDO_USER:-$USER}
echo "👤 현재 설치를 실행하는 계정: $ACTUAL_USER"

# 1. 한국 시간대(KST) 및 필수 패키지 설치
sudo timedatectl set-timezone Asia/Seoul
sudo apt-get update
sudo apt-get install -y docker.io docker-compose python3-pip python3-venv

# 2. env 파일 체크 및 줄바꿈 코드(CRLF) 자동 제거
if [ ! -f "env" ]; then
    echo "❌ env 파일을 먼저 작성해 주세요."
    exit 1
fi

# 윈도우 스타일 줄바꿈(\r)이 있다면 자동으로 제거하여 인터프리터 에러 예방
sed -i -e 's/\r$//' env
if [ -f ".env" ]; then
    sed -i -e 's/\r$//' .env
fi

# 환경 변수 로드
export $(grep -v '^#' env | xargs)

# 3. 폴더 구조 생성 및 소유권 변경
# root 권한으로 생성하되, 소유권을 실제 로그인한 사용자에게 돌려주어 권한 에러를 예방합니다.
sudo mkdir -p "$BASE_PATH"
sudo mkdir -p "$BACKUP_PATH"
sudo chown -R $ACTUAL_USER:$ACTUAL_USER "$BASE_PATH"
sudo chown -R $ACTUAL_USER:$ACTUAL_USER "$BACKUP_PATH"
chmod 755 "$BASE_PATH"

cd "$BASE_PATH"

# 4. 초기 JSON 생성
if [ ! -f "status.json" ]; then
    echo '{"last_backup": "-", "last_cleanup": "-", "last_restart": "-", "msg_id": null}' > status.json
    chown $ACTUAL_USER:$ACTUAL_USER status.json
fi

# 5. 서버 구동 (최신 이미지 체크)
echo "🚀 팰월드 서버 이미지를 업데이트하고 실행합니다..."
sudo docker-compose pull
sudo docker-compose up -d
echo "⏳ 서버 설치 중... 180초 대기"
sleep 180

# 6. 파이썬 가상환경 설정
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install discord.py python-dotenv psutil docker mcrcon requests

# 7. 시스템 서비스 등록
cat <<EOF | sudo tee /etc/systemd/system/palbot.service
[Unit]
Description=Palworld Discord Bot
After=docker.service

[Service]
User=$ACTUAL_USER
WorkingDirectory=$BASE_PATH
ExecStart=$BASE_PATH/venv/bin/python3 main.py
Restart=always
StandardOutput=append:$BASE_PATH/bot_output.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable palbot.service
sudo systemctl start palbot.service

echo "✅ 모든 설치가 완료되었습니다."