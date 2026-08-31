#!/usr/bin/env python3
"""
9RKM - 9Router Key Manager (2026-08-18)
Satu daemon menggantikan key-disable-daemon.py + daily-key-check.py + hourly-key-disable.py:
  - Thread scan 5s   : auto-OFF key error (logika kronologis daemon lama, identik)
  - Thread cycle 5 jam: reset ON semua non-retired + json_remove errorCode + retire >=50 siklus
  - Thread HTTP      : Web UI parity (status + toggle), Tailscale-only
Toggle di scope KV 'key_auto_off_toggle' - terpisah dari state 'hourly_key_disable'.
"""
import sys, os, time, json, sqlite3, html, datetime, threading
import urllib.request, urllib.error, urllib.parse
import http.server, socketserver
import pathlib, subprocess

DB = os.environ.get("ROUTER_DB", "/home/ubuntu/.9router/db/data.sqlite")
ENV = os.environ.get("ROUTER_ENV", "/home/ubuntu/scripts/daily-key-check.env")
UI_PATH = os.environ.get("RKM_UI_PATH", "/home/ubuntu/scripts/9rkm")
HTTP_HOST = os.environ.get("RKM_HTTP_HOST", "100.82.126.88")
HTTP_PORT = int(os.environ.get("RKM_HTTP_PORT", "8819"))
KV_SCOPE = "hourly_key_disable"
KV_KEY = "state"
TOGGLE_SCOPE = "key_auto_off_toggle"
TOGGLE_KEY = "state"
MAX_FAILED_CYCLES = 50
CYCLE_HOURS = 5
CYCLE_SECONDS = CYCLE_HOURS * 3600
SLEEP_INTERVAL = 5
REMAP_LOCK = "/tmp/9rkm-remap.lock"
REMAP_DEBOUNCE_SEC = 1800
REMAP_STALE_SEC = 1800
REMAP_LOG = "/tmp/aa_remap_last.log"
REMAP_PROBING = threading.Event()
REMAP_CYCLE_SCOPE = "aa_remap_cycle"
REMAP_CYCLE_KEY = "state"
COMBO_NAMES = ("Artificial-Analysis-Intelligence-Index", "Artificial-Analysis-Agentic-Index")
EC_HINT = {400: "Request salah", 401: "Kunci salah/expired", 402: "Saldo habis", 403: "Akses ditolak", 429: "Kuota habis"}

def _hint(ec):
    try:
        c = int(str(ec).strip())
    except Exception:
        return f"Error {ec}"
    if c in EC_HINT:
        return EC_HINT[c]
    if 500 <= c <= 599:
        return "Gangguan gateway"
    return f"Error {c}"

def log(msg):
    ts = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S WIB")
    print(f"{ts} | {msg}", flush=True)

def get_db():
    conn = sqlite3.connect(DB, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn

def get_iso_now():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    ms = now_utc.microsecond // 1000
    return now_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"

def get_cycle_id(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return int(now.timestamp()) // CYCLE_SECONDS

def get_cutoff_iso(hours=1):
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

def load_env():
    vals = {}
    if os.path.exists(ENV):
        with open(ENV, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.strip().partition("=")
                    vals[k.strip()] = v.strip()
    return vals

ALERTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_alerts.json")

def _web_alert(msg):
    # Telegram OFF 26 Agu 2026 (AGENTS.md 17.8) — alert pindah ke file Web
    try:
        import datetime as _dt
        arr = []
        if os.path.exists(ALERTS_PATH):
            try:
                arr = json.load(open(ALERTS_PATH, encoding="utf-8"))
            except Exception:
                arr = []
        arr.append({"ts": _dt.datetime.now().isoformat(timespec="seconds"), "source": "9rkm", "text": msg})
        json.dump(arr[-500:], open(ALERTS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    log(f"[web-alert] {msg}")

def notify(msg):
    _web_alert(msg)

def provider_label(provider, data=None):
    specific = data.get("providerSpecificData") if isinstance(data, dict) else None
    if isinstance(specific, dict):
        for key in ("prefix", "nodeName"):
            value = specific.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return provider or "unknown"

def _remap_lock_state():
    try:
        import fcntl
        descriptor = os.open(REMAP_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(descriptor)
    except Exception:
        return False

def _acquire_remap_lock():
    import fcntl
    descriptor = os.open(REMAP_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(descriptor, 0)
        os.write(descriptor, str(os.getpid()).encode())
        return descriptor
    except BlockingIOError:
        os.close(descriptor)
        return None

def _release_remap_lock(descriptor):
    import fcntl
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)

def _remap_snapshot(cursor):
    out = {"lastAt": None, "lastWib": None, "source": None, "intel": None, "agentic": None, "vision": None, "cacheAt": None, "cacheAgeH": None, "locked": False, "cooldownSec": 0}
    try:
        cursor.execute("SELECT value FROM kv WHERE scope=? AND key=?", ("aa_cache", "state"))
        r = cursor.fetchone()
        if r and r["value"]:
            import json as _js
            j = _js.loads(r["value"])
            out["cacheAt"] = j.get("at")
            if j.get("at"):
                try:
                    dt = datetime.datetime.fromisoformat(j["at"].replace("Z","+00:00"))
                    out["cacheAgeH"] = round((datetime.datetime.now(datetime.timezone.utc)-dt).total_seconds()/3600,1)
                except Exception:
                    pass
    except Exception:
        pass
    try:
        cursor.execute("SELECT value FROM kv WHERE scope=? AND key=?", ("aa_remap", "state"))
        r = cursor.fetchone()
        if r and r["value"]:
            import json as _js2
            j = _js2.loads(r["value"])
            out["lastAt"] = j.get("at")
            out["source"] = j.get("source")
            out["intel"] = j.get("intel")
            out["agentic"] = j.get("agentic")
            out["vision"] = j.get("vision")
            if j.get("at"):
                try:
                    dt = datetime.datetime.fromisoformat(j["at"].replace("Z","+00:00"))
                    wib = dt.astimezone(datetime.timezone(datetime.timedelta(hours=7)))
                    out["lastWib"] = wib.strftime("%d %b %H:%M WIB")
                except Exception:
                    pass
                try:
                    dt = datetime.datetime.fromisoformat(j["at"].replace("Z","+00:00"))
                    age = (datetime.datetime.now(datetime.timezone.utc)-dt).total_seconds()
                    remain = REMAP_DEBOUNCE_SEC - age
                    out["cooldownSec"] = int(remain) if remain>0 else 0
                except Exception:
                    pass
    except Exception:
        pass
    out["locked"] = _remap_lock_state()
    return out

def _combo_snapshot():
    conn = get_db()
    try:
        cur = conn.cursor()
        return {name: (cur.execute("SELECT models FROM combos WHERE name=?", (name,)).fetchone() or [None])[0] for name in COMBO_NAMES}
    finally:
        conn.close()

def _restore_combos(snapshot):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        for name, models in snapshot.items():
            if models is None:
                cur.execute("DELETE FROM combos WHERE name=?", (name,))
            else:
                cur.execute("UPDATE combos SET models=? WHERE name=?", (models, name))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _probe_combo(name):
    conn = get_db()
    try:
        row = conn.execute("SELECT key FROM apiKeys LIMIT 1").fetchone()
        api_key = row[0] if row else None
    finally:
        conn.close()
    if not api_key:
        return False
    payload = json.dumps({"model": name, "messages": [{"role": "user", "content": "Reply PONG only"}], "max_tokens": 64, "stream": False}).encode()
    request = urllib.request.Request("http://127.0.0.1:20128/v1/chat/completions", data=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
            return 200 <= response.status < 300
    except Exception:
        return False

def _cycle_state():
    conn = get_db()
    try:
        return load_state_from_db(conn.cursor(), REMAP_CYCLE_SCOPE, REMAP_CYCLE_KEY)
    finally:
        conn.close()

def _save_cycle_state(state):
    conn = get_db()
    try:
        save_state_to_db(conn.cursor(), state, REMAP_CYCLE_SCOPE, REMAP_CYCLE_KEY)
        conn.commit()
    finally:
        conn.close()

def _restart_router():
    result = subprocess.run(["pm2", "restart", "9router"], capture_output=True, text=True, timeout=30)
    return result.returncode == 0

def _run_remap(force=False):
    cycle_id = get_cycle_id()
    if not force and _cycle_state().get("successCycle") == cycle_id:
        return 0
    descriptor = _acquire_remap_lock()
    if descriptor is None:
        log("[Remap] locked skip")
        return 5
    snapshot = None
    try:
        REMAP_PROBING.set()
        _, _, reset_status = run_reset()
        if reset_status != "ok":
            return 4
        snapshot = _combo_snapshot()
        log("[Remap] start discovery cycle")
        child_env = {**os.environ, "AA_REMAP_LOCK_HELD": "1"}
        result = subprocess.run(
            ["/usr/bin/python3", "/home/ubuntu/scripts/9rkm/aa_rank.py", "--remap", "--no-vision"],
            capture_output=True,
            text=True,
            timeout=1200,
            env=child_env,
        )
        output = (result.stdout or "")[-16000:] + (result.stderr or "")[-4000:]
        pathlib.Path(REMAP_LOG).write_text(output, encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError(f"aa_rank exit {result.returncode}")
        if not _restart_router():
            raise RuntimeError("pm2 restart failed")
        if not all(_probe_combo(name) for name in COMBO_NAMES):
            raise RuntimeError("combo E2E failed")
        _save_cycle_state({"successCycle": cycle_id, "at": get_iso_now(), "status": "ok"})
        log("[Remap] verified E2E")
        return 0
    except Exception as error:
        log(f"[Remap] error {error}")
        if snapshot is not None:
            try:
                _restore_combos(snapshot)
                _restart_router()
                log("[Remap] combo rollback restored")
            except Exception as rollback_error:
                log(f"[Remap] rollback failed {rollback_error}")
        _save_cycle_state({"successCycle": _cycle_state().get("successCycle"), "attemptCycle": cycle_id, "at": get_iso_now(), "status": f"error:{error}"})
        return 4
    finally:
        REMAP_PROBING.clear()
        _release_remap_lock(descriptor)

def _run_remap_async(force=False):
    def worker():
        code = _run_remap(True)
        notify(f"Remap combo selesai force={force} exit={code} @ {get_iso_now()}")
    threading.Thread(target=worker, daemon=True).start()


# ---------- KV state ----------

def load_state_from_db(cursor, scope=KV_SCOPE, key=KV_KEY):
    cursor.execute("SELECT value FROM kv WHERE scope = ? AND key = ?;", (scope, key))
    row = cursor.fetchone()
    if row and row["value"]:
        try:
            return json.loads(row["value"])
        except Exception:
            pass
    return {}

def save_state_to_db(cursor, state, scope=KV_SCOPE, key=KV_KEY):
    json_str = json.dumps(state, indent=2)
    cursor.execute(
        "INSERT INTO kv (scope, key, value) VALUES (?, ?, ?) ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value;",
        (scope, key, json_str)
    )

# ---------- Toggle ----------

def get_toggle():
    conn = get_db()
    try:
        cur = conn.cursor()
        st = load_state_from_db(cur, TOGGLE_SCOPE, TOGGLE_KEY)
        return st.get("enabled", True), st
    finally:
        conn.close()

def set_toggle(enabled, by):
    conn = get_db()
    try:
        cur = conn.cursor()
        st = {"enabled": bool(enabled), "by": by, "at": get_iso_now()}
        save_state_to_db(cur, st, TOGGLE_SCOPE, TOGGLE_KEY)
        conn.commit()
        return st
    finally:
        conn.close()

# ---------- Scan 5s (auto-OFF) ----------



def candidates_from_error_code(active_map, requests_by_conn, cutoff_iso, bulk_at_iso=None, bulk_grace_iso=None):
    found = []
    for cid, info in active_map.items():
        d = info.get("data")
        if not isinstance(d, dict):
            continue
        ec = d.get("errorCode")
        if ec is None:
            continue
        try:
            if int(str(ec).strip()) == 400:
                lbl = str(info.get("label", "")).strip().lower()
                if lbl == "bynara":
                    continue
                last_err = str(d.get("lastError") or "")
                if "model rejected this request" in last_err:
                    continue
        except Exception:
            pass  # ponytail: 400 is payload-specific; Bynara=>skip always, others=>skip only if "model rejected" (request-invalid), not invalid-key 400 — credential 401/403/429 still OFF
        reqs = requests_by_conn.get(cid, [])
        success_ts = None
        for ts, status, _ in reversed(reqs):
            if str(status).strip().lower() == "success":
                success_ts = ts
                break
        err_ts = d.get("lastErrorAt")
        if not err_ts or err_ts < cutoff_iso:
            continue
        if bulk_at_iso and err_ts <= bulk_at_iso:
            continue
        if bulk_grace_iso and err_ts <= bulk_grace_iso:
            continue  # ponytail: grace 10s pasca-bulk; naikkan ke 30s jika gateway rewrite >10s, hapus jika mau strict err>bulk
        if success_ts and success_ts > err_ts:
            continue
        found.append({
            "connectionId": cid,
            "consecutive_errors": 1,
            "total_in_window": len(reqs),
            "last_reason": f"errorCode {ec} (state gateway)",
            "_source": "errorCode",
        })
    return found

def run_scan_tick():
    if REMAP_PROBING.is_set():
        return 0, "remap-probing"
    now_wib = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    today_wib = now_wib.strftime("%Y-%m-%d")
    cutoff_iso = get_cutoff_iso(1)
    now_iso = get_iso_now()
    bulk_at_iso = None
    bulk_grace_iso = None

    conn = get_db()
    try:
        cursor = conn.cursor()

        enabled, _ = get_toggle()
        if not enabled:
            save_state_to_db(cursor, load_state_from_db(cursor))
            conn.commit()
            return 0, "toggle-off"

        cursor.execute("SELECT id, provider, name, email, isActive, data, updatedAt FROM providerConnections WHERE isActive = 1;")
        active_map = {}
        for row in cursor.fetchall():
            try:
                d = json.loads(row["data"]) if row["data"] else {}
            except Exception:
                d = {}
            if not isinstance(d, dict):
                d = {}
            active_map[row["id"]] = {
                "provider": row["provider"], "label": provider_label(row["provider"], d), "name": row["name"] or row["provider"],
                "email": row["email"], "data": d, "updatedAt": row["updatedAt"] or ""
            }

        active_ids = set(active_map.keys())
        if not active_ids:
            return 0, "no-active"

        placeholders = ",".join("?" for _ in active_ids)
        sql = f"SELECT timestamp, connectionId, status, data FROM requestDetails WHERE connectionId IN ({placeholders}) AND timestamp >= ? AND timestamp <= ? ORDER BY timestamp ASC;"
        cursor.execute(sql, list(active_ids) + [cutoff_iso, now_iso])
        rd_rows = cursor.fetchall()

        requests_by_conn = {}
        for row in rd_rows:
            cid = row["connectionId"]
            requests_by_conn.setdefault(cid, []).append((row["timestamp"], row["status"], row["data"]))

        try:
            _bulk = load_state_from_db(cursor, BULK_SCOPE, BULK_KEY)
            bulk_at_iso = _bulk.get("at") if isinstance(_bulk, dict) else None
            if bulk_at_iso:
                try:
                    _dt = datetime.datetime.fromisoformat(bulk_at_iso.replace("Z", "+00:00"))
                    _dt_g = _dt + datetime.timedelta(seconds=10)
                    bulk_grace_iso = _dt_g.strftime("%Y-%m-%dT%H:%M:%S.") + f"{_dt_g.microsecond//1000:03d}Z"
                except Exception:
                    bulk_grace_iso = None
        except Exception:
            pass
        state = load_state_from_db(cursor)
        current_cycle = get_cycle_id()
        for cid, reqs in requests_by_conn.items():
            if reqs and str(reqs[-1][1]).strip().lower() == "success":
                if cid in state:
                    state[cid]["failed_cycles"] = 0
                    state[cid].pop("consecutive_off_days", None)
                    state[cid].pop("off_cycle_id", None)
                    state[cid].pop("counted_cycle_id", None)
                    state[cid]["is_retired"] = False

        candidates = candidates_from_error_code(active_map, requests_by_conn, cutoff_iso, bulk_at_iso, bulk_grace_iso)

        if not candidates:
            save_state_to_db(cursor, state)
            conn.commit()
            return 0, "no-candidates"

        cid_list = [c["connectionId"] for c in candidates]
        placeholders_upd = ",".join("?" for _ in cid_list)
        cursor.execute(f"UPDATE providerConnections SET isActive = 0, updatedAt = ? WHERE id IN ({placeholders_upd});", [now_iso] + cid_list)

        off_list_msg = []
        for c in candidates:
            cid = c["connectionId"]
            info = active_map.get(cid, {})
            prov = info.get("label", info.get("provider", "unknown"))
            name = info.get("name", prov)
            conn_state = state.get(cid, {
                "provider": prov, "name": name, "consecutive_off_days": 0,
                "last_off_date": "", "is_retired": False, "manual_off": False, "auto_off_ts": ""
            })
            conn_state["provider"] = prov
            conn_state["name"] = name
            conn_state["manual_off"] = False
            conn_state["auto_off_ts"] = now_iso
            conn_state["off_cycle_id"] = current_cycle
            if conn_state.get("counted_cycle_id") != current_cycle:
                conn_state["failed_cycles"] = conn_state.get("failed_cycles", conn_state.get("consecutive_off_days", 0)) + 1
                conn_state["counted_cycle_id"] = current_cycle
            conn_state.pop("consecutive_off_days", None)
            conn_state["last_off_date"] = today_wib
            state[cid] = conn_state
            src_label = "errorCode" if c.get("_source") == "errorCode" else f"{c['consecutive_errors']}x err"
            off_list_msg.append(f"• <b>{html.escape(prov)}</b> ({html.escape(name[:20])}) — {src_label} ({html.escape(c['last_reason'])}) [Siklus ke-{conn_state.get('failed_cycles', 1)}]")

        save_state_to_db(cursor, state)
        conn.commit()
        log(f"[Auto-OFF 5s] {len(cid_list)} key dimatikan instant.")
        ts_fmt = now_wib.strftime("%d %b %H:%M:%S WIB")
        notify(
            f"⚡ <b>9Router Auto-OFF ({ts_fmt})</b>\n\n"
            f"<b>{len(off_list_msg)}</b> key dimatikan (deteksi 5 detik):\n"
            + "\n".join(off_list_msg)
            + "\n\n<i>Key akan diuji/reset kembali pada siklus 5 jam berikutnya.</i>"
        )
        return len(cid_list), "off"
    except Exception as e:
        log(f"[-] Scan error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0, f"error:{e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


BULK_SCOPE = "rkm_bulk"
BULK_KEY = "last"

def bulk_activate_all(by="WebUI"):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM providerConnections;")
        ids = [r["id"] for r in cur.fetchall()]
        now = get_iso_now()
        if ids:
            ph = ",".join("?" for _ in ids)
            cur.execute(f"UPDATE providerConnections SET data = json_remove(data, '$.errorCode', '$.lastError', '$.lastErrorAt', '$.backoffLevel'), updatedAt = ? WHERE id IN ({ph}) AND json_valid(data);", [now] + ids)
            cur.execute(f"UPDATE providerConnections SET isActive = 1, updatedAt = ? WHERE id IN ({ph});", [now] + ids)
        state = load_state_from_db(cur)
        for cid in ids:
            st = state.get(cid, {})
            st["failed_cycles"] = 0
            st["is_retired"] = False
            st.pop("consecutive_off_days", None)
            st.pop("off_cycle_id", None)
            st.pop("counted_cycle_id", None)
            st.pop("manual_off", None)
            st.pop("manual_off_at", None)
            st.pop("auto_off_ts", None)
            state[cid] = st
        bulk = {"action": "activate_all", "by": by, "at": now, "n": len(ids)}
        save_state_to_db(cur, bulk, BULK_SCOPE, BULK_KEY)
        save_state_to_db(cur, state)
        conn.commit()
        log(f"[Bulk] ACTIVATE ALL {len(ids)} by {by}.")
        notify(f"Bulk ACTIVATE ALL -- {len(ids)} key by {by} @ {now}")
        return len(ids), bulk
    finally:
        conn.close()

def bulk_deactivate_all(by="WebUI"):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM providerConnections;")
        ids = [r["id"] for r in cur.fetchall()]
        now = get_iso_now()
        if ids:
            ph = ",".join("?" for _ in ids)
            cur.execute(f"UPDATE providerConnections SET isActive = 0, updatedAt = ? WHERE id IN ({ph});", [now] + ids)
        state = load_state_from_db(cur)
        for cid in ids:
            st = state.get(cid, {"failed_cycles": 0, "is_retired": False})
            st["manual_off"] = True
            st["manual_off_at"] = now
            st["manual_off_by"] = by
            state[cid] = st
        bulk = {"action": "deactivate_all", "by": by, "at": now, "n": len(ids)}
        save_state_to_db(cur, bulk, BULK_SCOPE, BULK_KEY)
        save_state_to_db(cur, state)
        conn.commit()
        log(f"[Bulk] DEACTIVATE ALL {len(ids)} by {by}.")
        notify(f"Bulk DEACTIVATE ALL -- {len(ids)} key by {by} @ {now}")
        return len(ids), bulk
    finally:
        conn.close()

# ---------- Cycle 5 jam (reset ON + retire) ----------

def reconcile_state_and_connections(conns, state):
    to_activate = []
    retired = []
    for c in conns:
        cid = c["id"]
        cstate = state.get(cid)
        if cstate and cstate.get("manual_off"):
            retired.append({**c, "failed_cycles": cstate.get("failed_cycles", 0)})
            continue
        if cstate:
            is_ret = cstate.get("is_retired", False)
            failed_cycles = cstate.get("failed_cycles", cstate.get("consecutive_off_days", 0))
            if is_ret or failed_cycles >= MAX_FAILED_CYCLES:
                cstate["is_retired"] = True
                cstate["failed_cycles"] = max(cstate.get("failed_cycles", 0), MAX_FAILED_CYCLES)
                cstate.pop("manual_off", None)
                cstate.pop("auto_off_ts", None)
                state[cid] = cstate
                retired.append({**c, "failed_cycles": cstate["failed_cycles"]})
                continue
            if "manual_off" in cstate or "auto_off_ts" in cstate:
                cstate.pop("manual_off", None)
                cstate.pop("auto_off_ts", None)
                state[cid] = cstate
            to_activate.append(c)
        else:
            to_activate.append(c)
    return to_activate, retired

def run_reset():
    conn = get_db()
    try:
        cursor = conn.cursor()
        enabled, _ = get_toggle()
        if not enabled:
            log(f"[Reset {CYCLE_HOURS}h] skip (toggle OFF).")
            return 0, 0, "toggle-off"

        cursor.execute("SELECT id, provider, name, email, isActive, data, updatedAt FROM providerConnections;")
        rows = cursor.fetchall()
        conns = []
        for r in rows:
            try:
                data = json.loads(r["data"]) if r["data"] else {}
            except Exception:
                data = {}
            conns.append({"id": r["id"], "provider": provider_label(r["provider"], data), "name": r["name"] or r["provider"], "isActive": r["isActive"], "updatedAt": r["updatedAt"]})

        state = load_state_from_db(cursor)
        to_activate, retired = reconcile_state_and_connections(conns, state)
        now_iso = get_iso_now()

        if to_activate:
            act_ids = [c["id"] for c in to_activate]
            placeholders = ",".join("?" for _ in act_ids)
            cursor.execute(
                f"UPDATE providerConnections SET data = json_remove(data, '$.errorCode', '$.lastError', '$.lastErrorAt', '$.backoffLevel'), updatedAt = ? WHERE id IN ({placeholders}) AND json_valid(data);",
                [now_iso] + act_ids
            )
            cursor.execute(
                f"UPDATE providerConnections SET isActive = 1, updatedAt = ? WHERE id IN ({placeholders});",
                [now_iso] + act_ids
            )
            log(f"ON: {len(to_activate)} koneksi diaktifkan kembali (state error dibersihkan).")

        if retired:
            ret_ids = [c["id"] for c in retired]
            placeholders = ",".join("?" for _ in ret_ids)
            cursor.execute(
                f"UPDATE providerConnections SET isActive = 0, updatedAt = ? WHERE id IN ({placeholders});",
                [now_iso] + ret_ids
            )
            log(f"RETIRED: {len(retired)} koneksi tetap OFF (>= {MAX_FAILED_CYCLES} siklus gagal).")

        save_state_to_db(cursor, state)
        conn.commit()
        return len(to_activate), len(retired), "ok"
    except Exception as e:
        log(f"[-] Reset error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0, 0, f"error:{e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass

def reset_cycle_thread():
    while True:
        current = get_cycle_id()
        if _cycle_state().get("successCycle") != current:
            code = _run_remap()
            time.sleep(300 if code else 30)
        else:
            time.sleep(30)

# ---------- Status snapshot (dipakai TG + Web) ----------

def status_snapshot():
    enabled, togg = get_toggle()
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS t, SUM(isActive) AS a FROM providerConnections;")
        r = cur.fetchone()
        total = r["t"] or 0
        active = r["a"] or 0
        cur.execute("SELECT id, provider, data FROM providerConnections WHERE isActive = 1;")
        provider_counts = {}
        for row in cur.fetchall():
            try:
                data = json.loads(row["data"]) if row["data"] else {}
            except Exception:
                data = {}
            label = provider_label(row["provider"], data)
            provider_counts[label] = provider_counts.get(label, 0) + 1
        by_prov = [{"provider": label, "count": count} for label, count in sorted(provider_counts.items(), key=lambda item: (-item[1], item[0]))]
        cur.execute("SELECT id, provider, name, email, isActive, data FROM providerConnections ORDER BY provider, name;")
        conn_rows = cur.fetchall()
        connection_labels = {}
        conn_data = {}
        for row in conn_rows:
            try:
                data = json.loads(row["data"]) if row["data"] else {}
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            connection_labels[row["id"]] = (provider_label(row["provider"], data), row["name"] or row["provider"])
            conn_data[row["id"]] = data
        state = load_state_from_db(cur)
        retired = []
        for cid, item in state.items():
            if item.get("is_retired"):
                label, fallback_name = connection_labels.get(cid, (item.get("provider", "unknown"), item.get("name", "-")))
                retired.append({"provider": label, "name": item.get("name") or fallback_name, "failed_cycles": item.get("failed_cycles", 0)})
        bulk_last = load_state_from_db(cur, BULK_SCOPE, BULK_KEY)
        keys = []
        for row in conn_rows:
            cid = row["id"]
            d = conn_data.get(cid, {})
            prov_label = provider_label(row["provider"], d)
            name = (row["name"] or "").strip() or (row["email"] or "").strip() or cid[:8]
            st = state.get(cid, {})
            failed = st.get("failed_cycles", st.get("consecutive_off_days", 0))
            if st.get("manual_off"):
                status = "Manual OFF"
            elif st.get("is_retired"):
                status = "Pensiun"
            elif not row["isActive"]:
                status = "OFF"
            else:
                status = "Aktif"
            ec = d.get("errorCode")
            last_err = d.get("lastError") or ""
            last_err = " ".join(str(last_err).split())[:80] if last_err else ""
            ket = "-"
            if ec is not None:
                ket = f"{ec} {_hint(ec)}"
                if last_err:
                    ket += f" · {last_err}"
                if failed:
                    ket += f" · S{failed}"
            elif not row["isActive"] and failed:
                ket = f"S{failed}"
            elif not row["isActive"] and last_err:
                ket = last_err
            keys.append({"key": name, "provider": prov_label, "status": status, "ket": ket})
        keys.sort(key=lambda x: (0 if x["status"] != "Aktif" else 1, x["provider"], x["key"]))
        cur_id = get_cycle_id()
        next_at = (cur_id + 1) * CYCLE_SECONDS
        now_sec = int(time.time())
        remaining = next_at - now_sec
        if remaining < 0:
            remaining = 0
        wib = datetime.datetime.fromtimestamp(next_at, tz=datetime.timezone(datetime.timedelta(hours=7)))
        cycle = {"nextAt": next_at, "remainingSec": remaining, "enabled": bool(enabled), "intervalSec": CYCLE_SECONDS, "wib": wib.strftime("%d %b %H:%M WIB")}
        remap = _remap_snapshot(cur)
        return {
            "enabled": enabled,
            "toggle": togg,
            "total": total,
            "active": active,
            "by_provider": by_prov,
            "retired_count": len(retired),
            "retired": retired,
            "bulk_last": bulk_last if bulk_last else None,
            "remap": remap,
            "keys": keys,
            "ts": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S WIB"),
            "cycle": cycle,
        }
    finally:
        conn.close()

# ---------- Telegram ----------
# Input TG (polling) DIHAPUS 2026-08-18: kontrak 1 poller/bot.
# FASE B (2026-08-20): input via bot-hub — update di-POST ke /api/tg.
# Notifikasi keluar tetap via notify() (sendMessage = push, bukan poll).

TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or os.environ.get("CHAT_ID") or load_env().get("CHAT_ID") or "355679325"
TG_KEYBOARD = {"inline_keyboard": [
    [{"text": "📊 STATUS", "callback_data": "rkm:status"}],
    [{"text": "✅ KEY MANAGER ON", "callback_data": "rkm:on"}],
    [{"text": "⛔ KEY MANAGER OFF", "callback_data": "rkm:off"}],
]}


def tg_api(method, payload):
    env = load_env()
    bot = env.get("BOT_TOKEN")
    if not bot:
        return None
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot}/{method}",
        data=urllib.parse.urlencode(payload).encode(),
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def tg_send(text, keyboard=False):
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = json.dumps(TG_KEYBOARD)
    try:
        if tg_api("sendMessage", payload):
            return
    except Exception:
        pass
    payload.pop("parse_mode", None)
    try:
        tg_api("sendMessage", payload)
    except Exception as e:
        log(f"[-] tg_send fail: {e}")


def tg_status_text():
    s = status_snapshot()
    provs = ", ".join(f"{p['provider']}={p['count']}" for p in s["by_provider"][:6])
    c = s.get("cycle") or {}
    rs = int(c.get("remainingSec", 0))
    hh = rs // 3600
    mm = (rs % 3600) // 60
    if c.get("enabled"):
        timer = f"Auto ON: {c.get('wib','-')} (in {hh}j {mm}m)"
    else:
        timer = f"Auto ON jeda (toggle OFF) - berikutnya jika ON: {c.get('wib','-')} ({hh}j {mm}m)"
    toggle = "🟢 ON" if s["enabled"] else "🔴 OFF"
    return (
        "⚙️ <b>9RKM — Key Manager</b>\n\n"
        f"Toggle: {toggle}\n"
        f"Key aktif: <b>{s['active']}/{s['total']}</b>\n"
        f"Provider: {provs or '-'}\n"
        f"Retired: {s['retired_count']}\n"
        f"{timer}\n\n"
        f"Threshold auto-retire: ≥{MAX_FAILED_CYCLES} siklus ({CYCLE_HOURS} jam/siklus)\n"
        f"⏱ {s['ts']}"
    )


def tg_handle(update):
    """Update TG dari bot-hub (POST /api/tg). Whitelist chat Dea."""
    cb = update.get("callback_query")
    if cb:
        if str(cb.get("from", {}).get("id")) != TG_CHAT_ID:
            return
        data = (cb.get("data") or "").strip()
        if data == "rkm:on":
            set_toggle(True, "TG")
            tg_send("✅ Key Manager <b>ON</b> — scan 5s + reset 5 jam aktif.", keyboard=True)
        elif data == "rkm:off":
            set_toggle(False, "TG")
            tg_send("⛔ Key Manager <b>OFF</b> — scan + reset berhenti.", keyboard=True)
        elif data == "rkm:status":
            tg_send(tg_status_text(), keyboard=True)
        else:
            log(f"[TG] callback tak dikenal: {data!r}")
        try:
            tg_api("answerCallbackQuery", {"callback_query_id": cb.get("id", "")})
        except Exception:
            pass
        return
    msg = update.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != TG_CHAT_ID:
        return
    low = (msg.get("text") or "").strip().lower()
    if low in ("/start", "/status"):
        tg_send(tg_status_text(), keyboard=True)
    elif low.startswith("/keymanager"):
        arg = low.replace("/keymanager", "").strip()
        if arg == "on":
            set_toggle(True, "TG")
            tg_send("✅ Key Manager <b>ON</b> — scan 5s + reset 5 jam aktif.", keyboard=True)
        elif arg == "off":
            set_toggle(False, "TG")
            tg_send("⛔ Key Manager <b>OFF</b> — scan + reset berhenti.", keyboard=True)
        else:
            tg_send(tg_status_text(), keyboard=True)
    else:
        tg_send("Perintah: /status · /keymanager on|off", keyboard=True)

# ---------- HTTP (Web UI parity) ----------

class RkmHandler(http.server.BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] in ("/", "/index.html"):
            p = os.path.join(UI_PATH, "index.html")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    self._html(f.read())
            else:
                self._json(500, {"error": "ui missing"})
            return
        if self.path.startswith("/api/status"):
            self._json(200, status_snapshot())
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.startswith("/api/tg"):
            self._json(410, {"error": "Telegram OFF - Web-only per AGENTS.md 17.8 (26 Agu 2026)"})
            return
        if self.path.startswith("/api/keys/activate_all"):
            n, bulk = bulk_activate_all("WebUI")
            self._json(200, {**status_snapshot(), "bulk_result": {"n": n, **bulk}})
            return
        if self.path.startswith("/api/keys/deactivate_all"):
            n, bulk = bulk_deactivate_all("WebUI")
            self._json(200, {**status_snapshot(), "bulk_result": {"n": n, **bulk}})
            return
        if self.path.startswith("/api/remap"):
            if self.path.startswith("/api/remap/log"):
                try:
                    txt = ""
                    if os.path.exists(REMAP_LOG):
                        txt = pathlib.Path(REMAP_LOG).read_text(encoding="utf-8")[-4000:]
                    self._json(200, {"log": txt})
                except Exception as e:
                    self._json(500, {"error": str(e)})
                return
            try:
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln).decode()) if ln else {}
            except Exception:
                body = {}
            force = bool(body.get("force"))
            conn2 = get_db()
            try:
                cur2 = conn2.cursor()
                rs = _remap_snapshot(cur2)
                if rs.get("locked"):
                    self._json(423, {"ok": False, "reason": "locked", "remap": rs})
                    return
                if rs.get("cooldownSec", 0) > 0 and not force:
                    self._json(429, {"ok": False, "reason": "cooldown", "cooldownSec": rs["cooldownSec"], "remap": rs})
                    return
            finally:
                try:
                    conn2.close()
                except Exception:
                    pass
            _run_remap_async(force=force)
            self._json(202, {"ok": True, "force": force, "msg": "remap started"})
            return
        if self.path.startswith("/api/toggle"):
            try:
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln).decode()) if ln else {}
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            if "enabled" not in body:
                self._json(400, {"error": "missing enabled"})
                return
            st = set_toggle(bool(body["enabled"]), "WebUI")
            log(f"[WebUI] toggle -> {'ON' if st['enabled'] else 'OFF'}.")
            self._json(200, status_snapshot())
            return
        self._json(404, {"error": "not found"})

    def log_message(self, *a):
        pass

def http_thread():
    os.makedirs(UI_PATH, exist_ok=True)
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.ThreadingTCPServer((HTTP_HOST, HTTP_PORT), RkmHandler) as srv:
            log(f"[WebUI] listening http://{HTTP_HOST}:{HTTP_PORT}/ (Tailscale only)")
            srv.serve_forever()
    except Exception as e:
        log(f"[-] HTTP error: {e}")

# ---------- Main ----------

def main():
    log("=== 9RKM — 9Router Key Manager started ===")
    enabled, _ = get_toggle()
    log(f"Toggle awal: {'ON' if enabled else 'OFF'}")
    threads = [
        threading.Thread(target=reset_cycle_thread, daemon=True),
        threading.Thread(target=http_thread, daemon=True),
    ]
    for t in threads:
        t.start()
    while True:
        try:
            run_scan_tick()
        except Exception as e:
            log(f"[-] Loop error: {e}")
        time.sleep(SLEEP_INTERVAL)

if __name__ == "__main__":
    main()
