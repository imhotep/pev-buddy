#!/usr/bin/env bash
# One-command VPS setup/update for PEV Buddy, served publicly through
# Tailscale Funnel. Only this app's port is exposed; everything else on the
# node stays tailnet-only.
#
#   bash deploy/setup.sh           # install or update
#   bash deploy/setup.sh --sync    # also refresh the data bundle
#
# Env overrides: APP_DIR (default ~/pev-buddy), PORT (default 8000), REPO.
set -euo pipefail

REPO="${REPO:-https://github.com/imhotep/pev-buddy.git}"
APP_DIR="${APP_DIR:-$HOME/pev-buddy}"
PORT="${PORT:-8000}"
FORCE_SYNC=0
[ "${1:-}" = "--sync" ] && FORCE_SYNC=1

# --- system deps (Debian/Ubuntu; no-op when already present) ---
if ! command -v git >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq git python3-venv
fi

# --- code ---
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"

python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
.venv/bin/pip install -q --no-deps .

# --- data (gitignored; built once, refreshed with --sync) ---
if [ "$FORCE_SYNC" = 1 ] || [ ! -f data/graph.npz ]; then
  .venv/bin/python -m pev_buddy.sync
fi

# --- systemd service (localhost only; Funnel provides the public edge) ---
sudo tee /etc/systemd/system/pev-buddy.service >/dev/null <<EOF
[Unit]
Description=PEV Buddy
After=network-online.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/uvicorn pev_buddy.api:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now pev-buddy
sudo systemctl restart pev-buddy

# --- public ingress ---
tailscale funnel --bg "$PORT"
tailscale funnel status
