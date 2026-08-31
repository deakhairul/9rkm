#!/usr/bin/env python3
"""
rkm_shadow.py - 9RKM v2 Shadow Observer (2026-09-01)

Membaca requestDetails + providerConnections.data tiap 5 detik,
mengklasifikasi error (eksplisit vs ambigu), menulis rkm_event,
mengevaluasi blast-radius breaker secara DRY-RUN (freeze hanya dicatat
di rkm_engine_state.freeze_shadow, TIDAK memblok apa pun karena engine
belum enforce), menghitung provider-incident kandidat, dan menandai
sukses produksi (shadow_healthy).

TIDAK PERNAH menulis: providerConnections, combos, settings, isActive.
State engine tetap enabled=false (shadow) -> semua mutasi nyata off.

systemd: 9rkm-shadow.service (terpisah dari 9rkm legacy).
"""
import os
import sys
import json
import time
import sqlite3
import datetime
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_spec = importlib.util.spec_from_file_location("rkm_state", os.path.join(HERE, "rkm_state.py"))
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

DB = os.environ.get("ROUTER_DB", "/home/ubuntu/.9router/db/data.sqlite")
POLL_SEC = int(os.environ.get("RKM_SHADOW_POLL", "5"))
WINDOW_MIN = int(os.environ.get("RKM_SHADOW_WINDOW_MIN", "60"))
CURSOR_KEY = "shadow_cursor"

# ---------- helpers ----------

def log(msg):
    ts = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S WIB")
    try:
        print(f"{ts} | [rkm-shadow] {msg}", flush=True)
    except UnicodeEncodeError:
        print(f"{ts} | [rkm-shadow] {msg}".encode("ascii", "replace").decode("ascii"), flush=True)

def load_cursor(conn):
    row = conn.execute("SELECT value FROM rkm_engine_state WHERE name='shadow_cursor'").fetchone()
    return json.loads(row["value"]) if row else {}

def save_cursor(conn, cur):
    conn.execute(
        "INSERT INTO rkm_engine_state(name,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        ("shadow_cursor", json.dumps(cur), R.now_iso()))
    conn.commit()

def last_error_of(conn, cid):
    """Baca errorCode/lastError/lastErrorAt fresh dari providerConnections.data (mutasi gateway)."""
    row = conn.execute("SELECT data FROM providerConnections WHERE id=?", (cid,)).fetchone()
    if not row or not row["data"]:
        return None
    try:
        d = json.loads(row["data"])
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    if d.get("errorCode") is None:
        return None
    return {"code": d.get("errorCode"), "body": d.get("lastError") or "", "at": d.get("lastErrorAt")}

def snapshot_active_errors(conn, cutoff_iso):
    """Error state gateway fresh (<= window) untuk key isActive=1."""
    rows = conn.execute(
        "SELECT id, provider, isActive, data FROM providerConnections").fetchall()
    out = {}
    for r in rows:
        if not r["isActive"]:
            continue
        try:
            d = json.loads(r["data"]) if r["data"] else {}
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        ec = d.get("errorCode")
        at = d.get("lastErrorAt")
        if ec is None or not at or at < cutoff_iso:
            continue
        out[r["id"]] = {"code": ec, "body": d.get("lastError") or "", "at": at, "provider": r["provider"]}
    return out

# ---------- provider incident heuristic (shadow) ----------

INCIDENT_MIN_KEYS = 2
INCIDENT_PCT = 50.0

def detect_incidents(conn, error_map):
    """>=50% key satu provider error eksplisit/ambigu dalam window -> incident kandidat (shadow)."""
    by_prov = {}
    for cid, e in error_map.items():
        by_prov.setdefault(e["provider"], []).append(cid)
    for prov, cids in by_prov.items():
        row = conn.execute("SELECT COUNT(*) c FROM providerConnections WHERE provider=?", (prov,)).fetchone()
        if not row or not row["c"]:
            continue
        if len(cids) >= INCIDENT_MIN_KEYS and 100.0 * len(cids) / row["c"] >= INCIDENT_PCT:
            R.record_event(conn, "shadow_incident_candidate", None, prov,
                           json.dumps({"keys": len(cids), "total": row["c"]}))

# ---------- main loop ----------

def tick(conn, cur):
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(minutes=WINDOW_MIN)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    err_map = snapshot_active_errors(conn, cutoff)
    # 1) klasifikasi setiap error fresh -> event shadow (idempotent per key+at via cursor dedup)
    seen_at = cur.get("last_error_at", {})
    obs = []
    for cid, e in err_map.items():
        key_state = conn.execute("SELECT * FROM rkm_key_state WHERE connection_id=?", (cid,)).fetchone()
        if key_state is None:
            continue
        if seen_at.get(cid) == e["at"]:
            continue  # sudah dicatat, tidak double
        explicit, reason = R.classify_error(e["code"], e["body"])
        R.record_event(conn,
                       "shadow_explicit" if explicit else "shadow_ambiguous",
                       cid, key_state["provider_node_id"],
                       json.dumps({"code": e["code"], "reason": reason}))
        seen_at[cid] = e["at"]
        if explicit:
            obs.append((cid, e["code"], e["body"]))
    cur["last_error_at"] = seen_at
    # 2) breaker dry-run
    if obs:
        would_freeze = R.breaker_evaluate(conn, [o[0] for o in obs])
        if would_freeze:
            R.record_event(conn, "shadow_breaker_would_freeze", None, None,
                           json.dumps({"candidates": len(set(o[0] for o in obs))}))
    # 3) incident kandidat
    detect_incidents(conn, err_map)
    # 4) sukses produksi -> shadow_healthy (sekali per key per window via cursor)
    seen_ok = cur.get("last_ok_at", {})
    rows = conn.execute(
        "SELECT connectionId, MAX(timestamp) m FROM requestDetails WHERE status='success' AND timestamp > ? GROUP BY connectionId",
        (cur.get("success_since") or cutoff,)).fetchall()
    for r in rows:
        cid = r["connectionId"]
        if not cid:
            continue
        ks = conn.execute("SELECT provider_node_id FROM rkm_key_state WHERE connection_id=?", (cid,)).fetchone()
        if ks is None:
            continue
        if seen_ok.get(cid) == r["m"]:
            continue
        R.record_event(conn, "shadow_prod_success", cid, ks["provider_node_id"], "")
        seen_ok[cid] = r["m"]
    cur["last_ok_at"] = seen_ok
    cur["success_since"] = R.now_iso()
    save_cursor(conn, cur)
    return len(obs)

def main():
    R.log = log
    conn = R.open_db(DB)
    R.ensure_schema(conn)
    cur = load_cursor(conn)
    log(f"shadow observer start poll={POLL_SEC}s window={WINDOW_MIN}m (nol mutasi produksi)")
    while True:
        try:
            n = tick(conn, cur)
            if n:
                log(f"tick: {n} error eksplisit baru diklasifikasi (shadow)")
        except Exception as e:
            log(f"tick error: {e}")
        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
