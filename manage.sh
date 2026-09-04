#!/usr/bin/env bash
set -euo pipefail
cmd=${1:-help}
case "$cmd" in
 status) docker compose ps ;;
 logs) docker compose logs -f --tail=200 app ;;
 start) docker compose up -d ;;
 stop) docker compose down ;;
 restart) docker compose restart ;;
 update) git pull --ff-only && docker compose build && docker compose run --rm app alembic upgrade head && docker compose up -d ;;
 migrate) docker compose run --rm app alembic upgrade head ;;
 backup) mkdir -p backups; umask 077; f="backups/backup-$(date -u +%Y%m%dT%H%M%SZ).sql"; docker compose exec -T db pg_dump -U shop shop | gzip >"$f.gz"; echo "$f.gz" ;;
 restore) [[ $# == 2 ]] || { echo 'usage: manage.sh restore FILE.sql.gz' >&2; exit 2; }; gzip -dc "$2" | docker compose exec -T db psql -U shop shop ;;
 doctor) docker compose config -q; test "$(stat -c %a .env)" = 600; curl -fsS http://127.0.0.1:8080/health/live; echo ;;
 *) echo 'usage: manage.sh {status|logs|start|stop|restart|update|migrate|backup|restore|doctor}' >&2; exit 2 ;;
esac

