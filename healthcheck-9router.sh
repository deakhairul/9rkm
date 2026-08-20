#!/bin/bash
# healthcheck-9router.sh — cek 9router port 20128, auto-restore kalau mati/salah port.
# Restart hanya lewat restart-9router.sh (PORT=20128) — script eksternal, tidak di repo ini.
# Cron: */5 * * * *

LOG=${HC_LOG:-/home/ubuntu/scripts/healthcheck-9router.log}
VAULT=${HC_VAULT:-/home/ubuntu/obsidian-vault}
STATE=/home/ubuntu/scripts/.hc-9r-state
TS=$(TZ=Asia/Jakarta date "+%Y-%m-%d %H:%M WIB")
MAXLOG=2000

log() { echo "$TS | $1" >> "$LOG"; }

# -- cek port 20128 (9router benar) --
if ss -tlnp 2>/dev/null | grep -q ':20128 '; then
  log "SEHAT port 20128 listening"
  exit 0
fi

log "!! port 20128 TIDAK listening — investigasi"

# -- jatuh ke port 3000? (pola insiden 2026-08-04) --
if ss -tlnp 2>/dev/null | grep -q ':3000 '; then
  log "!! 9router jatuh ke port 3000 — restore via restart-9router.sh"
fi

# -- restore --
if [ -x /home/ubuntu/restart-9router.sh ]; then
  /home/ubuntu/restart-9router.sh >> "$LOG" 2>&1
  RESTORE_OK=1
else
  log "!! restart-9router.sh tidak ada — SKIP auto-restore"
  RESTORE_OK=0
fi

# -- verifikasi pasca restore --
if ss -tlnp 2>/dev/null | grep -q ':20128 '; then
  log "RESTORE OK — port 20128 listening setelah restart"
else
  log "RESTORE GAGAL — port 20128 masih kosong"
fi

# -- notifikasi: vault note (optional) + telegram --
if [ -d "$VAULT" ]; then
  NOTE="$VAULT/03-Daily/insiden-9router-$(TZ=Asia/Jakarta date '+%Y-%m-%d').md"
  {
    echo "# Insiden 9Router — $TS"
    echo
    echo "Port 20128 mati, auto-restore dijalankan. Detail:"
    echo "- Script: healthcheck-9router.sh"
    echo "- Restart: restart-9router.sh (PORT=20128)"
    echo "- Hasil: $RESTORE_OK"
  } >> "$NOTE"
  ( cd "$VAULT" && git add -A 2>/dev/null && git commit -m "hc: insiden 9router auto-restore $TS" --quiet 2>/dev/null && git push --quiet 2>/dev/null ) &
  log "vault note + commit triggered"
fi

# -- telegram (creds dari env file eksternal, tidak di-commit) --
TG_ENV_FILE=${HC_TG_ENV:-/home/ubuntu/scripts/daily-key-check.env}
if [ -f "$TG_ENV_FILE" ]; then
  source <(grep -E "^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|TELEGRAM_TOPIC_ID|BOT_TOKEN|CHAT_ID)=" "$TG_ENV_FILE")
  TG_BOT=${TELEGRAM_BOT_TOKEN:-$BOT_TOKEN}
  TG_CHAT=${TELEGRAM_CHAT_ID:-$CHAT_ID}
  MSG="⚠️ 9Router down — auto-restore dijalankan ($TS). Hasil: $RESTORE_OK"
  curl -s -o /dev/null -m 10 -X POST "https://api.telegram.org/bot$TG_BOT/sendMessage" \
    --data-urlencode "chat_id=$TG_CHAT" \
    --data-urlencode "message_thread_id=$TELEGRAM_TOPIC_ID" \
    --data-urlencode "text=$MSG" 2>/dev/null
  log "telegram notify sent"
fi

# -- log rotate (simple) --
tail -n $MAXLOG "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit 0
