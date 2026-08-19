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

def notify(msg):
    env = load_env()
    bot = env.get("BOT_TOKEN")
    chat = env.get("CHAT_ID", "355679325")
    if not bot:
        return
    esc = html.escape(msg, quote=False)
    html_msg = esc.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>").replace("&lt;code&gt;", "<code>").replace("&lt;/code&gt;", "</code>").replace("&lt;i&gt;", "<i>").replace("&lt;/i&gt;", "</i>")
    for params in ({"parse_mode": "HTML", "text": html_msg}, {"text": msg}):
        data = urllib.parse.urlencode({"chat_id": chat, **params}).encode()
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{bot}/sendMessage", data=data, timeout=10) as r:
                if r.status == 200:
                    return
        except Exception:
            pass

def provider_label(provider, data=None):
    specific = data.get("providerSpecificData") if isinstance(data, dict) else None
    if isinstance(specific, dict):
        for key in ("prefix", "nodeName"):
            value = specific.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return provider or "unknown"

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

def get_response_error(data_str):
    if not data_str:
        return None, ""
    try:
        response = json.loads(data_str).get("response", {})
        if not isinstance(response, dict):
            return None, ""
        code = response.get("status")
        reason = response.get("error") or response.get("message") or ""
        if isinstance(reason, dict):
            code = code or reason.get("code")
            reason = reason.get("message") or reason.get("status") or json.dumps(reason)
        elif isinstance(reason, str):
            try:
                nested = json.loads(reason)
                error = nested.get("error", {}) if isinstance(nested, dict) else {}
                if isinstance(error, dict):
                    code = code or error.get("code")
                    reason = error.get("message") or error.get("status") or reason
            except Exception:
                pass
        try:
            code = int(code)
        except Exception:
            code = None
        return code, " ".join(str(reason).split())[:120]
    except Exception:
        return None, ""

def evaluate_consecutive_errors(requests_by_conn):
    candidates = []
    for conn_id, reqs in requests_by_conn.items():
        if not reqs:
            continue
        consecutive_err = 0
        last_reason = ""
        for ts, status, data_str in reversed(reqs):
            if str(status).strip().lower() != "error":
                break
            consecutive_err += 1
            if not last_reason:
                _, last_reason = get_response_error(data_str)
        if consecutive_err >= 3:
            candidates.append({
                "connectionId": conn_id,
                "consecutive_errors": consecutive_err,
                "total_in_window": len(reqs),
                "last_reason": last_reason or "Error beruntun",
            })
    return candidates

def candidates_from_error_code(active_map, requests_by_conn):
    found = []
    for cid, info in active_map.items():
        d = info.get("data")
        if not isinstance(d, dict):
            continue
        ec = d.get("errorCode")
        if ec is None:
            continue
        reqs = requests_by_conn.get(cid, [])
        success_ts = None
        for ts, status, _ in reversed(reqs):
            if str(status).strip().lower() == "success":
                success_ts = ts
                break
        err_ts = d.get("lastErrorAt") or info.get("updatedAt") or ""
        if success_ts and err_ts and success_ts > err_ts:
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
    now_wib = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    today_wib = now_wib.strftime("%Y-%m-%d")
    cutoff_iso = get_cutoff_iso(1)
    now_iso = get_iso_now()

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

        candidates = evaluate_consecutive_errors(requests_by_conn)
        ec_candidates = candidates_from_error_code(active_map, requests_by_conn)
        seen = {c["connectionId"] for c in candidates}
        for c in ec_candidates:
            if c["connectionId"] not in seen:
                candidates.append(c)
                seen.add(c["connectionId"])

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

# ---------- Cycle 5 jam (reset ON + retire) ----------

def reconcile_state_and_connections(conns, state):
    to_activate = []
    retired = []
    for c in conns:
        cid = c["id"]
        cstate = state.get(cid)
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
    last_cycle = get_cycle_id()
    while True:
        cur = get_cycle_id()
        if cur != last_cycle:
            run_reset()
            last_cycle = cur
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
        cur.execute("SELECT id, provider, name, data FROM providerConnections;")
        connection_labels = {}
        for row in cur.fetchall():
            try:
                data = json.loads(row["data"]) if row["data"] else {}
            except Exception:
                data = {}
            connection_labels[row["id"]] = (provider_label(row["provider"], data), row["name"] or row["provider"])
        state = load_state_from_db(cur)
        retired = []
        for cid, item in state.items():
            if item.get("is_retired"):
                label, fallback_name = connection_labels.get(cid, (item.get("provider", "unknown"), item.get("name", "-")))
                retired.append({"provider": label, "name": item.get("name") or fallback_name, "failed_cycles": item.get("failed_cycles", 0)})
        return {
            "enabled": enabled,
            "toggle": togg,
            "total": total,
            "active": active,
            "by_provider": by_prov,
            "retired_count": len(retired),
            "retired": retired,
            "ts": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S WIB"),
        }
    finally:
        conn.close()

# ---------- Telegram ----------
# Input TG (polling) DIHAPUS 2026-08-18: kontrak 1 poller/bot dipegang idx_report_bot
# (409 conflict). Notifikasi keluar tetap via notify() (sendMessage = push, bukan poll).
# Bot-hub (Fase B) akan menjadi poller tunggal semua project.

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
