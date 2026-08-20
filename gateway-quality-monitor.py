#!/usr/bin/env python3
"""
gateway-quality-monitor.py — sensor kualitas gateway 9Router (anti silent-degradation).
- Error-rate window (default 6 jam) dari requestDetails, provider publik yang
  diketahui di-blacklist dikecualikan (mereka error sendiri bukan masalah kita).
- rate > threshold -> alert Telegram + state dedupe (max 1x/5 jam).
- Canary: replikasi request tool-calling ke satu model -> harus HTTP 200.
  Gagal = regresi upstream -> alert.
- Args: --dry-run (print saja) | --hours N
Env: GQM_PUBLIC_PROVIDERS (comma-separated provider IDs yang dikecualikan),
     GQM_CANARY_MODEL (model canary; canary skip jika kosong).
State: ~/.9router/gateway-quality-state.json  Log: ~/scripts/gateway-quality.log
"""
import json, os, subprocess, sys, datetime, urllib.request, urllib.error

import tg_notify

DB = os.environ.get("ROUTER_DB", "/home/ubuntu/.9router/db/data.sqlite")
API = os.environ.get("GQM_API", "http://localhost:20128/v1/chat/completions")
STATE = os.environ.get("GQM_STATE", "/home/ubuntu/.9router/gateway-quality-state.json")
LOG = os.environ.get("GQM_LOG", "/home/ubuntu/scripts/gateway-quality.log")
# Provider publik milik pihak lain (error mereka bukan masalah konfigurasi kita)
PUBLIC_PROVIDERS = {p.strip() for p in os.environ.get("GQM_PUBLIC_PROVIDERS", "").split(",") if p.strip()}
CANARY_MODEL = os.environ.get("GQM_CANARY_MODEL", "")
THRESHOLD = 0.12
DEDUPE_H = 5
CANARY_TIMEOUT = 40

def db(sql):
    r = subprocess.run(["sqlite3", DB, sql], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"sqlite: {r.stderr}")
    return r.stdout

def router_key():
    try:
        k = db("SELECT key FROM apiKeys ORDER BY isActive DESC LIMIT 1;").strip()
        if k.startswith("sk-"):
            return k
    except Exception:
        pass
    return os.environ.get("ROUTER_KEY", "")

def log(msg):
    ts = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    line = f"{ts} | {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")
    print(line)

def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}

def save_state(s):
    json.dump(s, open(STATE, "w"))

def main():
    dry = "--dry-run" in sys.argv
    hours = 6
    for i, a in enumerate(sys.argv):
        if a == "--hours" and i + 1 < len(sys.argv):
            hours = int(sys.argv[i + 1])
    state = load_state()

    # ---- 1. Error rate provider kita ----
    excl = ""
    if PUBLIC_PROVIDERS:
        whitelist = ",".join(f"'{p}'" for p in PUBLIC_PROVIDERS)
        excl = f"AND provider NOT IN ({whitelist}) "
    rows = db(
        f"SELECT status, count(*) FROM requestDetails "
        f"WHERE timestamp > datetime('now','-{hours} hours') "
        f"{excl}GROUP BY status;"
    ).strip().splitlines()
    total = err = 0
    for line in rows:
        parts = line.split("|")
        if len(parts) != 2:
            continue
        s, c = parts[0], int(parts[1])
        total += c
        if s != "success":
            err += c
    rate = err / total if total else 0.0

    top = db(
        f"SELECT provider, model, count(*) FROM requestDetails "
        f"WHERE timestamp > datetime('now','-{hours} hours') "
        f"AND status != 'success' {excl}"
        f"GROUP BY provider, model ORDER BY 3 DESC LIMIT 5;"
    ).strip()

    log(f"[{hours}h] total={total} err={err} rate={rate*100:.1f}%")

    now = datetime.datetime.now().timestamp()
    last_alert = state.get("lastErrAlertTs", 0)
    if not dry and total >= 20 and rate > THRESHOLD and now - last_alert > DEDUPE_H * 3600:
        msg = (f"🚨 GATEWAY ERROR-RATE {rate*100:.1f}% ({err}/{total} dalam {hours}h)\n"
               f"Top error:\n{top or '(none)'}\n"
               f"Cek: ~/.9router/db requestDetails — kemungkinan regresi upstream/model.")
        tg_notify.notify(msg)
        state["lastErrAlertTs"] = now
        log("ALERT error-rate terkirim")
    elif dry and total >= 20 and rate > THRESHOLD:
        log("[dry-run] rate di atas threshold — akan alert")

    # ---- 2. Canary (request tool-calling ke satu model) ----
    if not CANARY_MODEL:
        log("canary: skip (GQM_CANARY_MODEL tidak diset)")
        save_state(state)
        return
    body = json.dumps({
        "model": CANARY_MODEL,
        "messages": [{"role": "system", "content": "You are a canary probe. Call the provided tool."},
                     {"role": "user", "content": '{"task":"ping"}'}],
        "tools": [{"type": "function", "function": {"name": "ping", "description": "ping",
                    "parameters": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}}],
        "tool_choice": "auto", "temperature": 0.2, "max_tokens": 2000,
    }).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": f"Bearer {router_key()}", "Content-Type": "application/json"})
    canary_ok = False
    canary_info = ""
    try:
        with urllib.request.urlopen(req, timeout=CANARY_TIMEOUT) as r:
            if r.status == 200:
                raw = r.read().decode()
                raw = raw.split("data: [DONE]")[0].strip()  # gateway appends SSE marker
                d = json.loads(raw)
                canary_ok = d.get("choices", [{}])[0].get("finish_reason") in ("tool_calls", "stop")
                canary_info = d["model"]
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        canary_info = str(e)
    log(f"canary {CANARY_MODEL}: {'OK' if canary_ok else 'FAIL'} ({canary_info})")

    last_fail = state.get("lastCanaryFailTs", 0)
    if not dry and not canary_ok and now - last_fail > DEDUPE_H * 3600:
        tg_notify.notify(f"🚨 CANARY GAGAL: {CANARY_MODEL} (tool_choice auto) — {canary_info}\n"
                         f"Regresi upstream kemungkinan — traffic gateway bisa terdampak. Cek gateway.")
        state["lastCanaryFailTs"] = now
        log("ALERT canary terkirim")

    save_state(state)

if __name__ == "__main__":
    main()
