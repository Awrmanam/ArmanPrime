#!/usr/bin/env bash
set -euo pipefail
command -v docker >/dev/null || { echo "Docker is required" >&2; exit 1; }
docker compose version >/dev/null
if [[ -e .env ]]; then echo '.env already exists; refusing to overwrite' >&2; exit 1; fi
NON_INTERACTIVE=${INSTALL_NON_INTERACTIVE:-false}
if [[ $NON_INTERACTIVE == true ]]; then
  : "${BOT_TOKEN:?BOT_TOKEN required}" "${ADMIN_ID:?ADMIN_ID required}" "${ORDER_CHAT:?ORDER_CHAT required}"
  SUPPORT_USERNAME=${SUPPORT_USERNAME:-}; TIMEZONE=${TIMEZONE:-UTC}; RUN_MODE=${RUN_MODE:-webhook}
  WEBHOOK_URL=${WEBHOOK_URL:-http://localhost:8080/test}; MONEY_UNIT=${MONEY_UNIT:-toman}
  MANUAL_USD_RATE=${MANUAL_USD_RATE:-}; CURRENCY_PROVIDER=${CURRENCY_PROVIDER:-}
  MIN_MARGIN=${MIN_MARGIN:-0}; KYC_MODE=${KYC_MODE:-manual}; CARD_POLICY=${CARD_POLICY:-single}
  CARD_COOLDOWN=${CARD_COOLDOWN:-7}; STRONG_MATCH=${STRONG_MATCH:-manual_review}
  FIRST_LIMIT=${FIRST_LIMIT:-0}; DAILY_LIMIT=${DAILY_LIMIT:-0}; PAYMENT_PROVIDER=${PAYMENT_PROVIDER:-}
  SECURE_PATH=${SECURE_PATH:-/var/lib/shopbot/secure}; RETENTION=${RETENTION:-90}
  MEMBERSHIP_CHANNEL=${MEMBERSHIP_CHANNEL:-}; BRAND_NAME=${BRAND_NAME:-}
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
read -rp 'Order notification chat ID: ' ORDER_CHAT
read -rp 'Support username (optional): ' SUPPORT_USERNAME
read -rp 'Timezone [UTC]: ' TIMEZONE; TIMEZONE=${TIMEZONE:-UTC}
read -rp 'Mode polling/webhook [polling]: ' RUN_MODE; RUN_MODE=${RUN_MODE:-polling}
WEBHOOK_URL=''; [[ $RUN_MODE == webhook ]] && read -rp 'HTTPS webhook URL: ' WEBHOOK_URL
read -rp 'Money unit toman/rial [toman]: ' MONEY_UNIT; MONEY_UNIT=${MONEY_UNIT:-toman}
read -rp 'Manual USD rate (blank = configure in bot): ' MANUAL_USD_RATE
read -rp 'Currency provider (optional): ' CURRENCY_PROVIDER
read -rp 'Minimum margin percent [0]: ' MIN_MARGIN; MIN_MARGIN=${MIN_MARGIN:-0}
read -rp 'KYC mode [manual]: ' KYC_MODE; KYC_MODE=${KYC_MODE:-manual}
read -rp 'Customer card policy single/multiple [single]: ' CARD_POLICY; CARD_POLICY=${CARD_POLICY:-single}
read -rp 'Card replacement cooldown days [7]: ' CARD_COOLDOWN; CARD_COOLDOWN=${CARD_COOLDOWN:-7}
read -rp 'Strong match policy [manual_review]: ' STRONG_MATCH; STRONG_MATCH=${STRONG_MATCH:-manual_review}
read -rp 'First purchase limit toman [0]: ' FIRST_LIMIT; FIRST_LIMIT=${FIRST_LIMIT:-0}
read -rp 'Daily limit toman [0]: ' DAILY_LIMIT; DAILY_LIMIT=${DAILY_LIMIT:-0}
read -rp 'Payment provider (optional): ' PAYMENT_PROVIDER
read -rp 'Secure document path [/var/lib/shopbot/secure]: ' SECURE_PATH; SECURE_PATH=${SECURE_PATH:-/var/lib/shopbot/secure}
read -rp 'Document retention days [90]: ' RETENTION; RETENTION=${RETENTION:-90}
read -rp 'Required membership channel (optional): ' MEMBERSHIP_CHANNEL
read -rp 'Brand name (optional): ' BRAND_NAME
fi
secret(){ python3 -c 'import secrets; print(secrets.token_urlsafe(48))'; }
POSTGRES_PASSWORD=$(secret); ENCRYPTION_KEY=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' 2>/dev/null || secret)
umask 077
cat >.env <<EOF
BOT_TOKEN=$BOT_TOKEN
ADMIN_TELEGRAM_USER_ID=$ADMIN_ID
ORDER_NOTIFICATION_CHAT_ID=$ORDER_CHAT
SUPPORT_USERNAME=$SUPPORT_USERNAME
TIMEZONE=$TIMEZONE
RUN_MODE=$RUN_MODE
WEBHOOK_URL=$WEBHOOK_URL
MONEY_UNIT=$MONEY_UNIT
MANUAL_USD_RATE=$MANUAL_USD_RATE
CURRENCY_PROVIDER=$CURRENCY_PROVIDER
MIN_MARGIN_PERCENT=$MIN_MARGIN
KYC_MODE=$KYC_MODE
CUSTOMER_CARD_POLICY=$CARD_POLICY
CARD_COOLDOWN_DAYS=$CARD_COOLDOWN
STRONG_MATCH_POLICY=$STRONG_MATCH
FIRST_PURCHASE_LIMIT_TOMAN=$FIRST_LIMIT
DAILY_LIMIT_TOMAN=$DAILY_LIMIT
PAYMENT_PROVIDER=$PAYMENT_PROVIDER
SECURE_FILE_PATH=$SECURE_PATH
DOCUMENT_RETENTION_DAYS=$RETENTION
MEMBERSHIP_CHANNEL=$MEMBERSHIP_CHANNEL
BRAND_NAME=$BRAND_NAME
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
for _ in {1..30}; do curl -fsS http://127.0.0.1:8080/health/live >/dev/null && break; sleep 2; done
curl -fsS http://127.0.0.1:8080/health/live >/dev/null
echo 'Installation complete. Send /setup to the bot; no catalog, terms, price, or brand was seeded.'
