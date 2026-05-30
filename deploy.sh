#!/bin/bash
set -e

PROJECT_DIR="/opt/InstagramYtBot"
SERVICE_NAME="instagramytbot"

echo "Deploying Instagram Downloader Bot..."

# Stop service if running
sudo systemctl stop $SERVICE_NAME || true

# Copy files
sudo mkdir -p $PROJECT_DIR
sudo cp -r . $PROJECT_DIR
sudo chown -R $USER:$USER $PROJECT_DIR

cd $PROJECT_DIR
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Create .env if not exists
if [ ! -f .env ]; then
    echo "Creating .env file - please edit with your tokens"
    cp .env.example .env
fi

# Setup systemd service
sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null <<EOF
[Unit]
Description=Instagram Downloader Telegram Bot
After=network.target

[Service]
User=$USER
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/venv/bin/python bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME
sudo systemctl start $SERVICE_NAME

echo "✅ Bot deployed and running!"