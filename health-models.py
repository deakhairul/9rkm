#!/usr/bin/env python3
"""
health-models.py — cek harian kesehatan model di combo Free (9Router VPS).
- Test tiap model via endpoint lokal (parallel, 8 worker).
- 3 hari gagal berturut-turut (non-429) = ALERT ke Telegram (Tidak auto-drop).
- Notify: vault note + Telegram.
State: ~/.9router/health-models-state.json
Log:   ~/scripts/health-models.log
"""
import json, os, subprocess, datetime, sys, urllib.request, concurrent.futures
import tg_notify

DB = "/home/ubuntu/.9router/db/data.sqlite"
COMBO = "Free"
API = "http://localhost:20128/v1/chat/completions"
STATE = "/home/ubuntu/.9router/health-models-state.json"
LOG = "/home/ubuntu/scripts/health-models.log"
MAX_FAIL = 3
TIMEOUT = 30
MAX_WORKERS = 8

def router_key():
    # Key 9Router di-rotate otomatis (backgroundTokenRefresh) — baca dari DB,
    # jangan hardcode. Fallback ke env ROUTER_KEY.
    try:
        k = db("SELECT key FROM apiKeys LIMIT 1;")
        if k.startswith("sk-"):
            return k
    except Exception:
        pass
    return os.environ.get("ROUTER_KEY", "")

def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    with open(LOG, "a") as f:
        f.write(f"{ts} | {msg}\n")
    print(msg)

def db(sql):
    r = subprocess.run(["sqlite3", DB, sql], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"sqlite: {r.stderr}")
    return r.stdout.strip()

def get_combo():
    raw = db(f"SELECT models FROM combos WHERE name='{COMBO}';")
    return json.loads(raw)

def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(s):
    with open(STATE, "w") as f:
        json.dump(s, f, indent=2)

def test_model(model, timeout=TIMEOUT):
    payload = json.dumps({"model": model,
                          "messages": [{"role": "user", "content": "ping"}],
                          "max_tokens": 4, "stream": False})
    key = router_key()
    if not key:
        return "fail"
    try:
        req = urllib.request.Request(API, data=payload.encode(),
                                     headers={"Authorization": f"Bearer {key}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
            msg = d.get("choices", [{}])[0].get("message", {})
            return "ok" if msg else "down"
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if "429" in str(e) or "rate_limit" in body or "FreeUsageLimit" in body:
            return "rate"
        if "No active credentials" in body or "model_not_found" in body:
            return "down"
        return "fail"
    except Exception as e:
        return "down" if timeout else "fail"

def notify_telegram(msg):
    tg_notify.notify(msg)

def main():
    try:
        combo = get_combo()
    except Exception as e:
        log(f"FATAL baca combo: {e}")
        sys.exit(1)
    
    state = load_state()
    alerts = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        results = list(ex.map(test_model, combo))

    for model, status in zip(combo, results):
        st = state.setdefault(model, {"fail_count": 0, "status": "unknown"})

        if status == "ok":
            if st["status"] == "alerted":
                alerts.append(f"✅ RESTORED: {model} hidup kembali (sebelumnya mati).")
            st.update({"fail_count": 0, "status": "ok"})
        elif status == "rate":
            st.update({"fail_count": 0, "status": "rate"})
        elif status in ("fail", "down"):
            st["fail_count"] = st.get("fail_count", 0) + 1
            if st["fail_count"] >= MAX_FAIL and st.get("status") != "alerted":
                st["status"] = "alerted"
                alerts.append(f"🚨 MATI: {model} gagal {st['fail_count']} hari berturut-turut. Tolong cek/hapus manual di dasbor 9Router!")
            elif st["status"] != "alerted":
                st["status"] = "fail"
        log(f"  {status:5} {model} (fail={st.get('fail_count',0)})")

    save_state(state)

    if alerts:
        msg = f"🩺 Health-Models {COMBO} Alert\n\n" + "\n".join(alerts)
        log(f"ALERT DIKIRIM: {len(alerts)} peringatan")
        # vault note
        vault = "/home/ubuntu/obsidian-vault"
        note = f"{vault}/03-Daily/health-models-alert-{datetime.date.today()}.md"
        with open(note, "w") as f:
            f.write(f"# Health Models Alert — {datetime.date.today()}\n\n" + "\n".join(alerts) + f"\n\nCek dasbor manual.\n")
        subprocess.run(["bash", "-c", f"cd {vault} && git add -A && git commit -m 'health-models: peringatan manual' --quiet && git push --quiet"],
                       timeout=30, capture_output=True)
        notify_telegram(msg)
    else:
        log(f"SEHAT semua ({len(combo)} model), tidak ada alert")

if __name__ == "__main__":
    main()