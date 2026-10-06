#!/usr/bin/env bash
# Safe code/data update for CI SSH deploys (and manual VPS use).
# Assumes the repo is already cloned at APP_DIR (run setup.sh once first).
# Does NOT reconfigure Tailscale Funnel / Caddy — ingress is already set.
#
#   bash deploy/remote-update.sh          # code-only update from release
#   bash deploy/remote-update.sh --sync   # also refresh the data bundle
#
# Env: APP_DIR (default ~/pev-buddy)
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/pev-buddy}"
FORCE_SYNC=0
[ "${1:-}" = "--sync" ] && FORCE_SYNC=1

if [ ! -d "$APP_DIR/.git" ]; then
  echo >&2 "APP_DIR=$APP_DIR is not a git checkout. Run deploy/setup.sh once first."
  exit 1
fi

cd "$APP_DIR"

git fetch origin
git checkout release
git pull --ff-only origin release

if [ ! -x .venv/bin/pip ]; then
  rm -rf .venv
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
.venv/bin/pip install -q --no-deps .

# Data is large/RAM-heavy — skip sync unless requested or bundle is missing.
# Building the bundle needs ~1.5 GB free RAM. On small/shared VPSs that can
# OOM — in that case build on a dev machine and push the result instead:
#   rsync -az data/ vps:pev-buddy/data/
if [ "$FORCE_SYNC" = 1 ] || [ ! -f data/graph.npz ]; then
  if ! .venv/bin/python -m pev_buddy.sync; then
    echo >&2 "sync failed (often OOM on small VPSs). Build locally and push:"
    echo >&2 "  rsync -az data/ <host>:$APP_DIR/data/"
    exit 1
  fi
fi

sudo systemctl enable pev-buddy
sudo systemctl restart pev-buddy

echo "Deployed $(git rev-parse --short HEAD) from release; pev-buddy restarted."
