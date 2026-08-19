#!/usr/bin/env python3
"""
gateway-quality-monitor.py — sensor kualitas gateway 9Router (anti silent-degradation).
- Error-rate window (default 6 jam) dari requestDetails untuk provider MILIK KITA
  (provider publik yang diketahui di-blacklist — mereka error sendiri bukan masalah kita).
- rate > 10% -> alert Telegram + state dedupe (max 1x/5 jam).
- Canary: replikasi request meridian (tools + tool_choice auto) ke ocg/deepseek-v4-flash
  -> harus HTTP 200. Gagal = regresi upstream -> alert.
- Args: --dry-run (print saja) | --hours N
State: ~/.9router/gateway-quality-state.json  Log: ~/scripts/gateway-quality.log
"""
import json, os, subprocess, sys, datetime, urllib.request, urllib.error

import tg_notify

DB = "/home/ubuntu/.9router/db/data.sqlite"
API = "http://localhost:20128/v1/chat/completions"
STATE = "/home/ubuntu/.9router/gateway-quality-state.json"
LOG = "/home/ubuntu/scripts/gateway-quality.log"
# Provider user publik (error mereka bukan masalah konfigurasi kita)
PUBLIC_PROVIDERS = {
    "openai-compatible-chat-766044ec-1137-4f87-ad22-42ccc998d898",  # zenmux
    "openai-compatible-chat-702fb81f-b075-4abf-82c4-c30d31087da5",  # madewgn
    "openai-compatible-chat-6df221de-7bac-4ba4-8297-464ab9493d1f",  # gorouter
    "openai-compatible-chat-531fd54b-b981-4796-95cb-dd9223c7d0e2",  # aiand
    "openai-compatible-chat-88ac9f54-9ccc-43a2-9a37-c0d6e098e1d4",  # inferx
    "opencode",  # zen (opencode.ai) — dipakai user publik
}
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
    whitelist = ",".join(f"'{p}'" for p in PUBLIC_PROVIDERS)
    rows = db(
        f"SELECT status, count(*) FROM requestDetails "
        f"WHERE timestamp > datetime('now','-{hours} hours') "
        f"AND provider NOT IN ({whitelist}) GROUP BY status;"
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
        f"AND status != 'success' AND provider NOT IN ({whitelist}) "
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

    # ---- 2. Canary (replikasi request meridian) ----
    body = json.dumps({
        "model": "ocg/deepseek-v4-flash",
        "messages": [{"role": "system", "content": "You are an autonomous DLMM LP agent. Role: SCREENER. Call deploy_position."},
                     {"role": "user", "content": '{"task":"pick best","candidates":[{"mint":"abc123"}]}'}],
        "tools": [{"type": "function", "function": {"name": "deploy_position", "description": "deploy position",
                    "parameters": {"type": "object", "properties": {"mint": {"type": "string"}}, "required": ["mint"]}}}],
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
    log(f"canary ocg/deepseek-v4-flash: {'OK' if canary_ok else 'FAIL'} ({canary_info})")

    last_fail = state.get("lastCanaryFailTs", 0)
    if not dry and not canary_ok and now - last_fail > DEDUPE_H * 3600:
        tg_notify.notify(f"🚨 CANARY GAGAL: ocg/deepseek-v4-flash (tool_choice auto) — {canary_info}\n"
                         f"Regresi upstream kemungkinan — meridian/charon bisa terdampak. Cek gateway.")
        state["lastCanaryFailTs"] = now
        log("ALERT canary terkirim")

    save_state(state)

if __name__ == "__main__":
    main()
