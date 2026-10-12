#!/usr/bin/env bash
# Deploy PEV Buddy to the production server, from a dev machine.
#
#   deploy/deploy.sh                 deploy DEPLOY_REF (code + data)
#   deploy/deploy.sh --ref v0.3.0    deploy a tag, branch or commit
#   deploy/deploy.sh --code-only     skip the data upload
#   deploy/deploy.sh --rollback      back to the previous release and its data
#   deploy/deploy.sh --dry-run       print what would run, change nothing
#   deploy/deploy.sh --yes           don't ask for confirmation
#
# Settings come from .env at the repository root (copy .env.sample). The
# server runs the app as a systemd unit behind Caddy (see README "Deploy");
# the data bundle is built here and pushed, since building it needs ~1.5 GB
# RAM.
#
# Steps: check the ref is pushed -> on the server, remember the current
# commit and snapshot data/ (hard links, cheap) -> check out the ref ->
# install dependencies (repairing a venv without pip) -> rsync data/ ->
# restart the service -> check /api/health and the public page.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ------------------------------------------------------------------ settings
if [ ! -f .env ]; then
  echo "No .env found. Copy the sample and fill it in:  cp .env.sample .env" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
. ./.env
set +a

: "${DEPLOY_HOST:?set DEPLOY_HOST in .env}"
DEPLOY_USER="${DEPLOY_USER:-}"
DEPLOY_SSH_PORT="${DEPLOY_SSH_PORT:-22}"
DEPLOY_APP_DIR="${DEPLOY_APP_DIR:-pev-buddy}"
DEPLOY_REF="${DEPLOY_REF:-main}"
DEPLOY_SERVICE="${DEPLOY_SERVICE:-pev-buddy}"
DEPLOY_APP_PORT="${DEPLOY_APP_PORT:-8765}"
DEPLOY_PUBLIC_URL="${DEPLOY_PUBLIC_URL:-}"
DEPLOY_DATA_DIR="${DEPLOY_DATA_DIR:-data}"
DEPLOY_KEEP_BACKUPS="${DEPLOY_KEEP_BACKUPS:-3}"

# ------------------------------------------------------------------- options
REF="$DEPLOY_REF"
SYNC_DATA=1
ROLLBACK=0
DRY_RUN=0
ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?--ref needs a value}"; shift ;;
    --code-only) SYNC_DATA=0 ;;
    --rollback) ROLLBACK=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -y | --yes) ASSUME_YES=1 ;;
    -h | --help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

TARGET="${DEPLOY_USER:+$DEPLOY_USER@}$DEPLOY_HOST"
SSH=(ssh -p "$DEPLOY_SSH_PORT" -o ServerAliveInterval=15)

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '    [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# Run a script on the server with the settings it needs; stdin is the script.
remote() {
  local env
  env="$(printf 'APP_DIR=%q REF=%q SERVICE=%q KEEP=%q SYNC_DATA=%q' \
    "$DEPLOY_APP_DIR" "$REF" "$DEPLOY_SERVICE" "$DEPLOY_KEEP_BACKUPS" "$SYNC_DATA")"
  if [ "$DRY_RUN" = 1 ]; then
    printf '    [dry-run] %s %s %s bash -s <<EOF\n' "${SSH[*]}" "$TARGET" "$env"
    sed 's/^/    [dry-run]   /'
    printf '    [dry-run] EOF\n'
  else
    "${SSH[@]}" "$TARGET" "$env bash -s"
  fi
}

confirm() {
  [ "$ASSUME_YES" = 1 ] || [ "$DRY_RUN" = 1 ] && return 0
  read -r -p "$1 [y/N] " ans
  [ "$ans" = y ] || [ "$ans" = Y ] || die "Aborted."
}

# The ?v=N the given ref's page loads, to check the public site serves it.
asset_version() {
  git show "$1:web/index.html" 2>/dev/null | grep -o 'app\.js?v=[0-9]*' | head -1 | cut -d= -f2
}

restart_and_check() {
  local want_version="$1"
  say "Restarting $DEPLOY_SERVICE"
  # -t: sudo may ask for a password.
  run "${SSH[@]}" -t "$TARGET" "sudo systemctl restart $DEPLOY_SERVICE"

  say "Health check (on the server)"
  run "${SSH[@]}" "$TARGET" \
    "curl -fsS --retry 30 --retry-connrefused --retry-delay 1 http://127.0.0.1:$DEPLOY_APP_PORT/api/health" \
    | { [ "$DRY_RUN" = 1 ] && cat || python3 -c 'import json,sys; h=json.load(sys.stdin); print(f"    {h[\"status\"]}: {h[\"nodes\"]} nodes, {h[\"edges\"]} edges, {h[\"stations\"]} chargers, release {h[\"release\"]}")'; }

  if [ -n "$DEPLOY_PUBLIC_URL" ] && [ -n "$want_version" ] && [ "$DRY_RUN" = 0 ]; then
    say "Public site: $DEPLOY_PUBLIC_URL"
    local live
    live="$(curl -fsS "$DEPLOY_PUBLIC_URL" | grep -o 'app\.js?v=[0-9]*' | head -1 | cut -d= -f2 || true)"
    if [ "$live" = "$want_version" ]; then
      echo "    serving v$live"
    else
      warn "public page serves v${live:-?}, expected v$want_version (check Caddy, or wait a few seconds and retry)"
    fi
  fi
}

# ------------------------------------------------------------------ rollback
if [ "$ROLLBACK" = 1 ]; then
  say "Rolling back $TARGET:$DEPLOY_APP_DIR to the release before the last deploy"
  confirm "Roll back production?"
  remote <<'EOF'
set -euo pipefail
cd "$APP_DIR"
[ -f .deploy-previous ] || { echo "No previous release recorded (.deploy-previous)"; exit 1; }
prev="$(cat .deploy-previous)"
echo "    code: $(git rev-parse --short HEAD) -> $(git rev-parse --short "$prev")"
git checkout -q --detach "$prev"
.venv/bin/python -m pip install -q -r requirements.txt && .venv/bin/python -m pip install -q --no-deps .
snap="$(cat .deploy-previous-data 2>/dev/null || echo none)"
if [ "$snap" != none ] && [ -d "$snap" ]; then
  echo "    data: restored from $snap"
  rsync -a --delete "$snap/" data/
else
  echo "    data: the last deploy didn't change it, left as is"
fi
EOF
  restart_and_check ""
  say "Rolled back."
  exit 0
fi

# -------------------------------------------------------------------- deploy
git fetch -q origin --tags
if git rev-parse -q --verify "origin/$REF^{commit}" >/dev/null; then
  COMMIT="$(git rev-parse "origin/$REF^{commit}")"
elif git rev-parse -q --verify "$REF^{commit}" >/dev/null; then
  COMMIT="$(git rev-parse "$REF^{commit}")"
  git branch -r --contains "$COMMIT" | grep -q . || die "$REF ($COMMIT) is not on GitHub yet: push it first."
else
  die "Unknown ref: $REF"
fi
[ -z "$(git status --porcelain --untracked-files=no)" ] || warn "Uncommitted local changes: they are not deployed (the server checks out $REF from GitHub)."

VERSION="$(asset_version "$COMMIT")"
say "Deploying $REF ($(git log -1 --format='%h %s' "$COMMIT")) to $TARGET:$DEPLOY_APP_DIR"
echo "    app shell v${VERSION:-?}; data upload: $([ "$SYNC_DATA" = 1 ] && echo "yes, from $DEPLOY_DATA_DIR/" || echo no)"
if [ "$SYNC_DATA" = 1 ]; then
  [ -f "$DEPLOY_DATA_DIR/graph.npz" ] || die "$DEPLOY_DATA_DIR/graph.npz missing: build the data first (python -m pev_buddy.sync), or use --code-only."
fi
confirm "Deploy to production?"

say "Updating code and dependencies on the server"
remote <<'EOF'
set -euo pipefail
cd "$APP_DIR"
# Remember where we were, for --rollback: the commit, and the data snapshot
# this deploy takes ("none" for a code-only deploy, which leaves data alone).
git rev-parse HEAD > .deploy-previous
echo none > .deploy-previous-data
if [ "$SYNC_DATA" = 1 ] && [ -d data ]; then
  # Hard-link snapshot: instant and nearly free on disk. rsync replaces
  # changed files (new inode), so the snapshot keeps the old versions.
  mkdir -p backups
  snap="backups/data-$(date +%Y%m%d-%H%M%S)"
  cp -al data "$snap"
  echo "$snap" > .deploy-previous-data
  echo "    data snapshot: $snap"
  ls -1d backups/data-* | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do rm -rf "$old"; done
fi
git fetch -q --tags origin
if git rev-parse -q --verify "origin/$REF^{commit}" >/dev/null; then
  git checkout -q "$REF" 2>/dev/null || git checkout -q -b "$REF" "origin/$REF"
  git merge -q --ff-only "origin/$REF"
else
  git checkout -q --detach "$REF"
fi
echo "    code: $(git log -1 --format='%h %s')"
# A venv made without pip (uv, or before python3-venv was installed).
.venv/bin/python -m pip --version >/dev/null 2>&1 || .venv/bin/python -m ensurepip --upgrade >/dev/null
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m pip install -q --no-deps .
echo "    dependencies installed"
EOF

if [ "$SYNC_DATA" = 1 ]; then
  say "Uploading $DEPLOY_DATA_DIR/ (only what changed)"
  run rsync -az -e "ssh -p $DEPLOY_SSH_PORT" "$DEPLOY_DATA_DIR/" "$TARGET:$DEPLOY_APP_DIR/data/"
fi

restart_and_check "$VERSION"
say "Deployed $REF. Roll back with: deploy/deploy.sh --rollback"
