#!/usr/bin/env bash
# Build the dashboard, copy the app and test videos to the Pi, and restart it.
# Usage (Git Bash, macOS or Linux):  tools/deploy_pi.sh [user@host]
# Needs SSH key login to the Pi and the one-time setup in README ("部署到 Raspberry Pi").
set -euo pipefail
# In PowerShell, `bash` is WSL, which cannot resolve raspberrypi.local (mDNS). Hand over to Git Bash.
if grep -qi microsoft /proc/version 2>/dev/null; then
  git_bash="/mnt/c/Program Files/Git/bin/bash.exe"
  if [ -x "$git_bash" ]; then
    exec "$git_bash" "$(wslpath -m "$0")" "$@"
  fi
  echo "WSL cannot resolve raspberrypi.local; run this from Git Bash, or pass user@<Pi IP>" >&2
  exit 1
fi
HOST="${1:-kelsier@raspberrypi.local}"
cd "$(dirname "$0")/.."

npm --prefix frontend run build
echo "Copying app to $HOST:~/bime01"
tar --exclude=__pycache__ -czf - aquarium frontend/dist deploy pyproject.toml uv.lock README.md \
  | ssh "$HOST" 'set -e; mkdir -p ~/bime01; cd ~/bime01; rm -rf aquarium frontend; tar -xzf - 2>/dev/null'

if [ -d .data/videos ]; then
  echo "Copying test videos"
  tar -cf - -C .data/videos . | ssh "$HOST" 'mkdir -p ~/bime01/.data/videos && tar -xf - -C ~/bime01/.data/videos 2>/dev/null'
fi

ssh "$HOST" 'bash -s' <<'REMOTE'
set -e
cd ~/bime01
if systemctl is-enabled --quiet aquarium 2>/dev/null; then
  sudo systemctl restart aquarium
  echo "Restarted systemd service"
else
  pkill -f "^\.venv/bin/python -m uvicorn" || true
  sleep 2
  AQUARIUM_SOURCE=picamera nohup .venv/bin/python -m uvicorn aquarium.main:app --host 127.0.0.1 --port 8000 \
    > .data/server.log 2>&1 < /dev/null &
  echo "Started in background (log: ~/bime01/.data/server.log)"
fi
for _ in $(seq 20); do
  sleep 1
  if state=$(curl -sf 127.0.0.1:8000/api/state); then
    echo "$state" | head -c 220; echo
    exit 0
  fi
done
echo "Server did not answer; check the log" >&2
exit 1
REMOTE
