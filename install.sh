#!/usr/bin/env bash
# Set up veilglass on this machine (ThinkPad or Surface).
set -euo pipefail
cd "$(dirname "$0")"
command -v uv >/dev/null || { echo "install uv first: sudo pacman -S uv"; exit 1; }
uv venv --quiet .venv
uv pip install --quiet --python .venv/bin/python bleak aiohttp
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/veilglass.service <<UNIT
[Unit]
Description=veilglass nearby radio device monitor
After=bluetooth.target network.target

[Service]
ExecStart=$PWD/.venv/bin/python $PWD/server.py --notify
Restart=on-failure

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
echo "installed. start it:  systemctl --user enable --now veilglass"
echo "open the glass:       ./kiosk.sh"
