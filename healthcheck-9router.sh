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

PORT_OK=0
if ss -tlnp 2>/dev/null | grep -q ':20128 '; then
  log "SEHAT port 20128 listening"
  PORT_OK=1
fi
if [ "$PORT_OK" -eq 1 ]; then
  : # sehat — lanjut RSS check di bawah
else

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

fi

# -- RSS monitor (pisah dari meridian — 2026-08-21) --
RSS_MB_THRESHOLD=${HC_RSS_MB_THRESHOLD:-500}
RSS_GROWTH_PCT=${HC_RSS_GROWTH_PCT:-50}
RSS_STATE=${HC_RSS_STATE:-/tmp/9router-healthcheck-state.json}
RSS_ALERT_COOLDOWN_S=${HC_RSS_ALERT_COOLDOWN_S:-900}
check_rss() {
  RSS_MB=$(pm2 jlist 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); m={p['name']:p for p in d}; p=m.get('9router'); print(p['monit']['memory']//1024//1024 if p else 0)" 2>/dev/null)
  [ -z "$RSS_MB" ] && return 0
  ALERT=""; NOW_TS=$(date +%s)
  if [ "$RSS_MB" -gt "$RSS_MB_THRESHOLD" ] 2>/dev/null; then ALERT="RSS ${RSS_MB}MB > ${RSS_MB_THRESHOLD}MB"; fi
  if [ -f "$RSS_STATE" ]; then
    PREV_RSS=$(python3 -c "import json; print(json.load(open('$RSS_STATE')).get('rss_mb',0))" 2>/dev/null || echo 0)
    PREV_ALERT=$(python3 -c "import json; print(json.load(open('$RSS_STATE')).get('alert',''))" 2>/dev/null || echo "")
    PREV_TS=$(python3 -c "import json; print(json.load(open('$RSS_STATE')).get('alert_ts',0))" 2>/dev/null || echo 0)
    if [ "$PREV_RSS" -gt 0 ] 2>/dev/null && [ "$RSS_MB" -gt 0 ] 2>/dev/null; then
      GROWTH=$(( (RSS_MB - PREV_RSS)*100 / PREV_RSS ))
      if [ "$GROWTH" -gt "$RSS_GROWTH_PCT" ] 2>/dev/null; then ALERT="${ALERT:+$ALERT; }RSS grew ${GROWTH}% (${PREV_RSS}MB->${RSS_MB}MB)"; fi
    fi
    echo "{\"rss_mb\":$RSS_MB,\"alert\":\"$ALERT\",\"alert_ts\":$NOW_TS}" > "$RSS_STATE.tmp" && mv "$RSS_STATE.tmp" "$RSS_STATE"
    if [ -n "$ALERT" ]; then
      MSG="🚨 [9router-healthcheck] $ALERT"
      if [ "$MSG" = "$PREV_ALERT" ] && [ $((NOW_TS-PREV_TS)) -lt "$RSS_ALERT_COOLDOWN_S" ] 2>/dev/null; then log "RSS alert dedup cooldown"; return 0; fi
      python3 -c "import json; d=json.load(open('$RSS_STATE')); d['alert']='$MSG'; d['alert_ts']=$NOW_TS; open('$RSS_STATE','w').write(json.dumps(d))" 2>/dev/null
    fi
  else
    echo "{\"rss_mb\":$RSS_MB,\"alert\":\"$ALERT\",\"alert_ts\":$NOW_TS}" > "$RSS_STATE"
  fi
  if [ -n "$ALERT" ]; then
    TG_ENV_FILE2=${HC_TG_ENV:-/home/ubuntu/scripts/daily-key-check.env}
    if [ -f "$TG_ENV_FILE2" ]; then
      source <(grep -E "^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|TELEGRAM_TOPIC_ID|BOT_TOKEN|CHAT_ID)=" "$TG_ENV_FILE2")
      TG_BOT2=${TELEGRAM_BOT_TOKEN:-$BOT_TOKEN}; TG_CHAT2=${TELEGRAM_CHAT_ID:-$CHAT_ID}
      MSG2="🚨 [9router-healthcheck] $ALERT"
      curl -s -o /dev/null -m 10 -X POST "https://api.telegram.org/bot$TG_BOT2/sendMessage" --data-urlencode "chat_id=$TG_CHAT2" --data-urlencode "message_thread_id=$TELEGRAM_TOPIC_ID" --data-urlencode "text=$MSG2" 2>/dev/null
      log "RSS telegram sent: $ALERT"
    fi
  fi
}

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
# RSS check runs even when port sehat (growth detection needs every run)
check_rss 2>/dev/null || true

# -- log rotate (simple) --
tail -n $MAXLOG "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
exit 0
