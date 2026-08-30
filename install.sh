#!/usr/bin/env bash
set -euo pipefail
command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }
docker compose version >/dev/null
if [[ -e .env ]]; then echo '.env already exists; refusing to overwrite' >&2; exit 1; fi
NON_INTERACTIVE=${INSTALL_NON_INTERACTIVE:-false}
if [[ $NON_INTERACTIVE == true ]]; then
  : "${BOT_TOKEN:?BOT_TOKEN required}" "${ADMIN_ID:?ADMIN_ID required}"
  ORDER_CHAT=${ORDER_CHAT:-$ADMIN_ID}; RUN_MODE=${RUN_MODE:-polling}
  WEBHOOK_URL=${WEBHOOK_URL:-}
  SECURE_PATH=${SECURE_PATH:-/var/lib/shopbot/secure}; BRAND_NAME=${BRAND_NAME:-}
else
read -rsp 'Bot token: ' BOT_TOKEN; echo
fi
if [[ ${SKIP_BOT_VALIDATION:-false} != true ]]; then python3 - "$BOT_TOKEN" <<'PY'
import json, sys, urllib.request
token=sys.argv[1]
try:
    data=json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=10))
    if not data.get("ok"): raise RuntimeError
except Exception:
    raise SystemExit("Bot token validation failed")
PY
fi
if [[ $NON_INTERACTIVE != true ]]; then
read -rp 'Admin Telegram user ID: ' ADMIN_ID
read -rp 'Order notification chat ID [same as Owner]: ' ORDER_CHAT; ORDER_CHAT=${ORDER_CHAT:-$ADMIN_ID}
RUN_MODE=polling; WEBHOOK_URL=''; SECURE_PATH=/var/lib/shopbot/secure; BRAND_NAME=''
fi
secret(){ openssl rand -base64 48 | tr -d '\n'; }
command -v openssl >/dev/null || { echo 'openssl is required' >&2; exit 1; }
POSTGRES_PASSWORD=$(secret); ENCRYPTION_KEY=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')
umask 077
cat >.env <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_TELEGRAM_USER_ID=$ADMIN_ID
ORDER_NOTIFICATION_CHAT_ID=$ORDER_CHAT
RUN_MODE=$RUN_MODE
WEBHOOK_URL=$WEBHOOK_URL
WEBHOOK_SECRET=$(secret)
SECURE_FILE_PATH=$SECURE_PATH
BRAND_NAME=$BRAND_NAME
FEATURE_WALLET=false
FEATURE_REFERRALS=false
FEATURE_COOPERATION=false
FEATURE_MEMBERSHIP_CHECK=false
PRICE_QUOTE_TTL_MINUTES=30
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
DATABASE_URL=postgresql+asyncpg://shop:$POSTGRES_PASSWORD@db:5432/shop
REDIS_URL=redis://redis:6379/0
ENCRYPTION_KEY=$ENCRYPTION_KEY
HMAC_KEY=$(secret)
CALLBACK_KEY=$(secret)
EOF
chmod 600 .env
docker compose build
docker compose up -d db redis
docker compose run --rm app alembic upgrade head
docker compose up -d
for _ in {1..60}; do curl -fsS http://127.0.0.1:8080/health/ready >/dev/null && break; sleep 2; done
if ! curl -fsS http://127.0.0.1:8080/health/ready >/dev/null; then
  docker compose logs --tail=200 app >&2; exit 1
fi
echo 'Installation complete. Send /admin to the bot; no catalog, terms, price, or brand was seeded.'
