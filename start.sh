#!/usr/bin/env sh
set -eu
PROJECT_DIRECTORY=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIRECTORY"
command -v docker >/dev/null 2>&1 || { echo 'Install Docker Engine/Desktop and Compose first.' >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo 'Docker is not running.' >&2; exit 1; }
docker compose version >/dev/null
case "${1:-start}" in
  stop) docker compose stop; echo 'Stopped. Database data is preserved.'; exit 0 ;;
  status) docker compose ps; exit 0 ;;
  start) ;;
  *) echo 'Usage: sh start.sh [start|stop|status]' >&2; exit 1 ;;
esac
echo 'Building images. The first run can take several minutes...'
docker compose build
docker compose run --rm --no-deps --user "$(id -u):$(id -g)" \
  --volume "$PROJECT_DIRECTORY:/workspace" --entrypoint python api /workspace/scripts/prepare_local.py
docker compose up -d --wait postgres api web
docker compose exec -T api uv run --no-sync python -m app.cli.bootstrap
docker compose up -d
echo 'Ready: http://localhost:8081 | Admin: http://localhost:8081/admin | API: http://localhost:8000/docs'
echo 'Admin key: ADMIN_API_TOKEN in .env. Existing settings are preserved.'
echo 'Without API keys the catalog and ordinary search still work.'
