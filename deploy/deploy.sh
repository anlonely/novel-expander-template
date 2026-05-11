#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${SSH_HOST:?set SSH_HOST (server ip/hostname)}"
: "${SSH_USER:=root}"
: "${SSH_PORT:=22}"
: "${REMOTE_DIR:=/opt/novel-expander}"

echo "==> Syncing to ${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}"
rsync -az --delete \
  --exclude '.git' \
  --exclude '.env' \
  --exclude '__pycache__' \
  --exclude '.venv' \
  --exclude 'venv' \
  --exclude 'data/novels.db' \
  --exclude 'data/exports' \
  --exclude 'data/settings.json' \
  --exclude 'data/prompts.json' \
  --exclude 'data/api_profiles.json' \
  --exclude 'data/config-backups' \
  -e "ssh -p ${SSH_PORT}" \
  "${ROOT_DIR}/" "${SSH_USER}@${SSH_HOST}:${REMOTE_DIR}/"

echo "==> Starting docker compose"
ssh -p "${SSH_PORT}" "${SSH_USER}@${SSH_HOST}" "cd '${REMOTE_DIR}' && docker compose up -d --build"

echo "==> Done"
