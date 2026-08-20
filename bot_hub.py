#!/usr/bin/env python3
"""
BOT HUB — poller tunggal semua bot Telegram (Control Surface Standard, Fase B).
Satu thread getUpdates per token (bot-hub-registry.json); setiap update
di-POST ke target HTTP lokal per project. Project memproses + balas sendiri
lewat token-nya masing-masing. Offset bookkeeping = tanggung jawab hub.

Usage:
  python3 bot_hub.py                  # live: poll + forward
  python3 bot_hub.py --dry            # poll + log saja, TANPA forward
  python3 bot_hub.py --bots 9rkm,idx  # batasi ke id registry tertentu
"""
import json
import os
import sys
import time
import threading
import datetime
import urllib.request

HUB_DIR = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(HUB_DIR, "bot-hub-registry.json")
LOG_PATH = os.path.join(HUB_DIR, "bot_hub.log")
DRY = "--dry" in sys.argv
ONLY = []
if "--bots" in sys.argv:
    ONLY = [s.strip() for s in sys.argv[sys.argv.index("--bots") + 1].split(",") if s.strip()]


def log(msg):
    line = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S WIB") + f" | {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_registry():
    with open(REGISTRY_PATH, encoding="utf-8") as f:
        reg = json.load(f)
    projects = reg.get("projects", [])
    if ONLY:
        projects = [p for p in projects if p.get("id") in ONLY]
    return projects


def read_token(entry):
    tf = entry.get("token_file", "")
    var = entry.get("token_var", "BOT_TOKEN")
    if not tf or not os.path.exists(tf):
        return None
    with open(tf, encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith(f"{var}="):
                return line.split("=", 1)[1].strip()
    return None


def tg(token, method, payload, timeout=60):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def update_text(update):
    msg = update.get("message") or update.get("edited_message") or {}
    return (msg.get("text") or "").strip()


def update_callback_data(update):
    cb = update.get("callback_query") or {}
    return (cb.get("data") or "").strip()


def match_entry(entries, update):
    """Routing murni: text/callback di-match ke patterns entry (substring).
    Update tanpa teks (foto dsb) hanya cocok dengan pattern '*'. None = no-match."""
    text = update_text(update) or update_callback_data(update) or "<media>"
    for e in entries:
        for pat in e.get("patterns", []):
            if pat == "*" or (pat and pat in text):
                return e
    return None


def post_update(entry, update):
    """Dispatch POST ke target. Bounded retry 1x, lalu log error + drop."""
    body = json.dumps(update).encode()
    last_err = None
    for _ in range(2):
        try:
            req = urllib.request.Request(
                entry["target"], data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=55) as r:
                r.read()
                return True
        except Exception as e:
            last_err = e
            time.sleep(1)
    log(f"[{entry.get('id')}] dispatch FAIL -> {entry['target']}: {last_err}")
    return False


def worker(entry, bot_entries):
    eid = entry.get("id", "?")
    token = read_token(entry)
    if not token:
        log(f"[{eid}] token hilang ({entry.get('token_file')}:{entry.get('token_var')}) — thread stop.")
        return
    username = "?"
    try:
        me = tg(token, "getMe", {})
        username = me.get("result", {}).get("username", "?")
    except Exception as e:
        log(f"[{eid}] getMe FAIL: {e} — lanjut poll.")
    log(f"[{eid}] thread start: @{username} dry={DRY}")
    offset = None
    while True:
        try:
            payload = {"timeout": 50}
            if offset:
                payload["offset"] = offset
            data = tg(token, "getUpdates", payload, timeout=55)
            for u in data.get("result", []):
                offset = u["update_id"] + 1
                kind = "cb" if u.get("callback_query") else "msg"
                text = (update_text(u) or update_callback_data(u))[:60]
                if DRY:
                    log(f"[{eid}] DRY {kind}: {text!r} (no forward)")
                    continue
                tgt = match_entry(bot_entries, u)
                if tgt is None:
                    log(f"[{eid}] no-match {kind} {text!r} — drop")
                    cb = u.get("callback_query")
                    if cb and cb.get("id"):
                        try:
                            tg(token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
                        except Exception:
                            pass
                    continue
                log(f"[{eid}] dispatch {kind} {text!r} -> {tgt['target']}")
                post_update(tgt, u)
        except Exception as e:
            log(f"[{eid}] poll error: {e}")
            time.sleep(5)
        time.sleep(1)


def main():
    projects = load_registry()
    if not projects:
        log("registry kosong / filter --bots tak ada cocok — stop.")
        return 1
    log(f"=== bot-hub start: {len(projects)} bot, dry={DRY}, only={ONLY or 'all'} ===")
    by_bot = {}
    for p in projects:
        by_bot.setdefault(p.get("bot", "").lower(), []).append(p)
    for p in projects:
        t = threading.Thread(
            target=worker,
            args=(p, by_bot[p.get("bot", "").lower()]),
            daemon=True,
        )
        t.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main())
