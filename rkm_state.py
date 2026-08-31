#!/usr/bin/env python3
"""
rkm_state.py - 9RKM v2 Key Health State Machine (2026-09-01)

Domain (ADR 0004..0006):
  Desired State  : Enabled/Disabled            -> milik operator (Dea)
  Routing State  : Enabled/Disabled            -> milik Key Health Engine (proyeksi tunggal isActive)
  Key Health     : Unknown/Recovering/Healthy/Unhealthy
  Health Reason  : Auth/Billing/Quota/Provider/Unknown
  Provider Incident : Open/Recovering/Closed   -> tidak mencemari Key Health individual

Urutan engine (ADR 0002 revisi):
  1. Observasi error -> classifier (eksplisit vs ambigu)
  2. Eksplisit account/key -> Unhealthy(+retryAt); ambigu -> canary exact-key (registry)
  3. Canary lolos -> Recovering + routing lease 15m/3 request
  4. Sukses produksi connectionId sama + generation sama -> Healthy
  Blast-radius breaker: >=20% global atau >=50% provider (min 3 global / 2 provider)
  dalam rolling 60s -> freeze mutasi baru; lepas setelah quiet 5m + canary pulih.

SHADOW MODE default ON: semua evaluasi hanya dicatat, tidak menyentuh
providerConnections. Enforce via rkm_engine_state.
Pure stdlib. DB sama dengan 9Router (tabel namespaced rkm_*).
"""
import os
import json
import time
import math
import sqlite3
import hashlib
import datetime
import threading

DB = os.environ.get("ROUTER_DB", "/home/ubuntu/.9router/db/data.sqlite")

# ---------- schema v2 (idempotent) ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS rkm_key_state (
  connection_id TEXT PRIMARY KEY,
  provider_node_id TEXT NOT NULL,
  desired TEXT NOT NULL DEFAULT 'Enabled' CHECK (desired IN ('Enabled','Disabled')),
  routing TEXT NOT NULL DEFAULT 'Disabled' CHECK (routing IN ('Enabled','Disabled')),
  health TEXT NOT NULL DEFAULT 'Unknown' CHECK (health IN ('Unknown','Recovering','Healthy','Unhealthy')),
  health_reason TEXT CHECK (health_reason IN ('Auth','Billing','Quota','Provider','Unknown') OR health_reason IS NULL),
  retry_at TEXT,
  failure_streak INTEGER NOT NULL DEFAULT 0,
  recovery_gen INTEGER NOT NULL DEFAULT 0,
  lease_expires TEXT,
  lease_budget INTEGER NOT NULL DEFAULT 0,
  cred_fingerprint TEXT,
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL DEFAULT 'engine'
);
CREATE INDEX IF NOT EXISTS idx_rkm_key_provider ON rkm_key_state(provider_node_id);
CREATE INDEX IF NOT EXISTS idx_rkm_key_routing ON rkm_key_state(routing);

CREATE TABLE IF NOT EXISTS rkm_provider_incident (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  incident_domain TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Open' CHECK (status IN ('Open','Recovering','Closed')),
  reason TEXT,
  opened_at TEXT NOT NULL,
  closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rkm_incident_domain ON rkm_provider_incident(incident_domain, status);

CREATE TABLE IF NOT EXISTS rkm_engine_state (
  name TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rkm_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  connection_id TEXT,
  provider_node_id TEXT,
  detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rkm_event_ts ON rkm_event(ts);
CREATE INDEX IF NOT EXISTS idx_rkm_event_kind ON rkm_event(kind);
"""

ENG_HEALTH = "key_health_engine"
ENG_COMBO = "combo_remapper"
ENG_VISION = "vision_remapper"

# ---------- util ----------

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.datetime.now(datetime.timezone.utc).microsecond//1000:03d}Z"

def iso_add(ts, seconds):
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
    return (dt + datetime.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.") + f"{(dt + datetime.timedelta(seconds=seconds)).microsecond//1000:03d}Z"

def iso_past(ts, seconds=None):
    if not ts:
        return False
    try:
        dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return False
    if seconds is None:
        return dt <= datetime.datetime.now(datetime.timezone.utc)
    return dt <= datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)

def fingerprint(api_key):
    if not api_key:
        return None
    return hashlib.sha256(("rkm1:" + str(api_key)).encode()).hexdigest()[:16]

def open_db(db_path=None):
    conn = sqlite3.connect(db_path or DB, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn

def log(msg):
    ts = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S WIB")
    safe = f"{ts} | [rkm-state] {msg}"
    try:
        print(safe, flush=True)
    except UnicodeEncodeError:
        print(safe.encode("ascii", "replace").decode("ascii"), flush=True)

# ---------- engine state ----------

def get_engine(conn, name):
    row = conn.execute("SELECT value FROM rkm_engine_state WHERE name=?", (name,)).fetchone()
    return json.loads(row["value"]) if row else {}

def set_engine(conn, name, state, by="engine"):
    conn.execute(
        "INSERT INTO rkm_engine_state(name,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (name, json.dumps(state), now_iso()))
    conn.commit()

def engines_offline(conn, names=(ENG_HEALTH, ENG_COMBO, ENG_VISION)):
    """True jika SEMUA engine disebut sedang tidak enforce (kill-switch aman)."""
    for name in names:
        st = get_engine(conn, name)
        if st.get("enabled", False):
            return False
    return True

# ---------- migration / bootstrap ----------

def provider_of_connection(conn, connection_id):
    row = conn.execute("SELECT provider FROM providerConnections WHERE id=?", (connection_id,)).fetchone()
    return row["provider"] if row else None

def bootstrap(conn, by="migration"):
    """Idempotent. Legacy diabaikan: semua key Desired=Enabled, Routing diproyeksikan
    dari isActive saat ini (routing aktif lanjut jalan; OFF tetap OFF sampai canary),
    Health=Unknown. Counter legacy tidak dipercaya (audit-only)."""
    rows = conn.execute("SELECT id, provider, isActive, data FROM providerConnections").fetchall()
    ts = now_iso()
    for r in rows:
        try:
            data = json.loads(r["data"]) if r["data"] else {}
        except Exception:
            data = {}
        fp = fingerprint(data.get("apiKey"))
        conn.execute(
            """INSERT INTO rkm_key_state(connection_id, provider_node_id, desired, routing, health,
               cred_fingerprint, updated_at, updated_by)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(connection_id) DO NOTHING""",
            (r["id"], r["provider"], "Enabled", "Enabled" if r["isActive"] else "Disabled", "Unknown", fp, ts, by))
    # incidentDomain bootstrap: node provider = domain (upgrade saat PrefixMap diganti node id)
    conn.commit()
    return len(rows)

def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()

# ---------- classifier ----------

AUTH_EXPLICIT = (
    "invalid api key", "invalid_api_key", "unauthorized", "incorrect api key",
    "api key not valid", "authentication", "credential", "the bound service account is deleted",
    "api key expired", "token expired", "no payment method on file", "add a payment method",
    "creditserror", "insufficient credits", "insufficient_credit", "payment required to access",
    "premium_required", "insufficient ba lance", "account ov", "user's credit limit is insufficient",
    "pre-deduction failed", "free quota has been exhausted", "exhausted your free",
)
BILLING_EXPLICIT = (
    "payment", "billing", "top up", "top-up", "quota has been exhausted", "exhausted",
    "credit limit", "insufficient credits", "plan", "subscription or extra usage", "upgrade for",
    "separate fee", "prepayment credits are depleted",
)
AUTH_MARKERS = (
    "invalid api key", "invalid_api_key", "unauthorized", "incorrect api key",
    "api key not valid", "authentication_error", "the bound service account is deleted",
    "api key expired", "token expired", "credentialserror", "creditserror",
    "no payment method", "service account", "account is disabled", "suspended",
)

def classify_error(error_code, error_body):
    """-> (explicit: bool, reason: 'Auth'|'Billing'|'Quota'|'Provider'|'Unknown')
    Eksplisit = bukti langsung kredensial/account mati (bisa langsung Unhealthy).
    Ambigu (model down, 5xx, 429 rate, 404 model, 400 request) -> butuh canary."""
    code = 0
    try:
        code = int(str(error_code).strip())
    except Exception:
        code = 0
    body = (str(error_body) or "").lower()
    if any(m in body for m in AUTH_MARKERS):
        if any(m in body for m in ("creditserror", "no payment method", "payment", "insufficient credit",
                                    "credit limit", "pre-deduction", "prepayment", "premium_required")):
            return True, "Billing"
        if any(m in body for m in ("free quota", "exhausted", "quota has been", "separate fee",
                                    "subscription or extra usage", "upgrade for")):
            return True, "Quota"
        return True, "Auth"
    if code in (401, 402, 403):
        # 401/402 hampir selalu account; 403 sering permission model — cek body dulu
        if "model" in body or "not available" in body or "denied access to model" in body:
            return False, "Provider"
        return True, "Billing" if code == 402 else "Auth"
    if code in (400, 404, 410, 429, 500, 502, 503, 504):
        return False, "Provider" if code >= 500 else "Unknown"
    return False, "Unknown"

# ---------- canary registry ----------

def canary_for(conn, provider_node_id):
    """Adapter per provider: dict(kind=..., ...) atau None bila Unsupported.
    Default: endpoint account /v1/models dengan apiKey key terkait (tanpa inference)."""
    row = conn.execute(
        "SELECT data FROM providerConnections WHERE provider=? AND isActive=1 LIMIT 1",
        (provider_node_id,)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["data"]) or {}
    except Exception:
        return None
    base = ((data.get("providerSpecificData") or {}).get("baseUrl") or "").rstrip("/")
    key = data.get("apiKey")
    if base and key:
        return {"kind": "models", "url": base + "/models", "key": key}
    return None

# ---------- breaker ----------

BREAKER_WINDOW_SEC = 60
BREAKER_GLOBAL_PCT = 20.0
BREAKER_PROVIDER_PCT = 50.0
BREAKER_MIN_GLOBAL = 3
BREAKER_MIN_PROVIDER = 2
BREAKER_QUIET_SEC = 300

def breaker_active(conn):
    st = get_engine(conn, ENG_HEALTH)
    fr = st.get("freeze", {})
    if not fr.get("active"):
        return False
    if iso_past(fr.get("until")):
        conn.execute("DELETE FROM rkm_event WHERE kind='breaker_candidate'")
        set_engine(conn, ENG_HEALTH, {**st, "freeze": {"active": False, "reason": "quiet-window-elapsed"}})
        return False
    return True

def breaker_evaluate(conn, candidate_ids):
    """Evaluasi blast-radius kandidat batch ini. True = freeze (batch tidak dimutasi)."""
    total = conn.execute("SELECT COUNT(*) c FROM providerConnections").fetchone()["c"]
    if not candidate_ids or not total:
        return False
    distinct = len(set(candidate_ids))
    pct = 100.0 * distinct / total
    if distinct >= BREAKER_MIN_GLOBAL and pct >= BREAKER_GLOBAL_PCT:
        return True
    by_prov = {}
    for cid in set(candidate_ids):
        p = provider_of_connection(conn, cid)
        if p:
            by_prov.setdefault(p, set()).add(cid)
    ptotal = {}
    for p in by_prov:
        ptotal[p] = conn.execute("SELECT COUNT(*) c FROM providerConnections WHERE provider=?", (p,)).fetchone()["c"]
    for p, cids in by_prov.items():
        if len(cids) >= BREAKER_MIN_PROVIDER and ptotal.get(p) and 100.0 * len(cids) / ptotal[p] >= BREAKER_PROVIDER_PCT:
            return True
    return False

def breaker_freeze(conn, reason, by="engine"):
    st = get_engine(conn, ENG_HEALTH)
    set_engine(conn, ENG_HEALTH, {
        **st,
        "freeze": {"active": True, "reason": reason, "at": now_iso(),
                   "until": iso_add(now_iso(), BREAKER_QUIET_SEC)},
    }, by)
    record_event(conn, "breaker_frozen", detail=json.dumps({"reason": reason}))

# ---------- event ----------

def record_event(conn, kind, connection_id=None, provider_node_id=None, detail=""):
    conn.execute(
        "INSERT INTO rkm_event(ts,kind,connection_id,provider_node_id,detail) VALUES(?,?,?,?,?)",
        (now_iso(), kind, connection_id, provider_node_id, str(detail)[:2000]))
    conn.commit()

# ---------- backoff ----------

BACKOFF_BASE = 60      # 1 menit
BACKOFF_MAX = 5 * 3600  # 5 jam

def next_retry(streak, last_retry_at=None):
    """Exponential + jitter; honor Retry-After/kebijakan provider lewat retry_at eksplisit bila ada."""
    if last_retry_at:
        try:
            base = datetime.datetime.fromisoformat(last_retry_at.replace("Z", "+00:00"))
            elapsed = (datetime.datetime.now(datetime.timezone.utc) - base).total_seconds()
            if elapsed < 60:
                return last_retry_at
        except Exception:
            pass
    n = max(1, int(streak))
    delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** min(n - 1, 12)))
    delay = delay * (0.75 + 0.5 * (int(time.time() * 1000) % 100) / 100.0)  # jitter 25%
    return iso_add(now_iso(), int(delay))

# ---------- core: observe + transition (shadow-aware) ----------

LEASE_SEC = 15 * 60
LEASE_BUDGET = 3

def key_row(conn, connection_id):
    return conn.execute("SELECT * FROM rkm_key_state WHERE connection_id=?", (connection_id,)).fetchone()

def observe_error(conn, connection_id, error_code, error_body, shadow=None):
    """Satu observasi error gateway. Kembalikan aksi yang diambil (dict).
    Di shadow mode: hanya evaluasi + event, tanpa mutasi routing/health Unhealthy."""
    if shadow is None:
        st = get_engine(conn, ENG_HEALTH)
        shadow = not st.get("enabled", False)
    ks = key_row(conn, connection_id)
    if ks is None:
        return {"action": "unknown-connection"}
    explicit, reason = classify_error(error_code, error_body)
    if ks["desired"] == "Disabled":
        return {"action": "manual-disabled", "explicit": explicit, "reason": reason}
    if not explicit:
        # ambigu: model/provider/rate -> bukan bukti key rusak. Bisa: canary, tapi
        # policy: diam (Model Health milik 9Router). Catat event saja.
        record_event(conn, "ambiguous_error", connection_id, ks["provider_node_id"],
                     json.dumps({"code": error_code, "reason": reason}))
        return {"action": "observe-only", "explicit": False, "reason": reason}
    # eksplisit -> Unhealthy + routing off + retryAt (kecuali breaker freeze)
    if shadow:
        record_event(conn, "shadow_unhealthy_candidate", connection_id, ks["provider_node_id"],
                     json.dumps({"code": error_code, "reason": reason}))
        return {"action": "shadow-unhealthy", "explicit": True, "reason": reason}
    conn.execute(
        """UPDATE rkm_key_state SET health='Unhealthy', health_reason=?, retry_at=?,
           failure_streak=failure_streak+1, recovery_gen=recovery_gen+1,
           lease_expires=NULL, lease_budget=0, updated_at=?, updated_by='engine'
           WHERE connection_id=?""",
        (reason, next_retry(ks["failure_streak"] + 1), now_iso(), connection_id))
    conn.execute("UPDATE providerConnections SET isActive=0, updatedAt=? WHERE id=?", (now_iso(), connection_id))
    conn.commit()
    record_event(conn, "key_unhealthy", connection_id, ks["provider_node_id"],
                 json.dumps({"code": error_code, "reason": reason}))
    return {"action": "unhealthy", "explicit": True, "reason": reason}

def observe_batch(conn, observations, shadow=None):
    """Batch observasi dengan blast-radius breaker. Setiap item:
    (connection_id, error_code, error_body)."""
    if shadow is None:
        st = get_engine(conn, ENG_HEALTH)
        shadow = not st.get("enabled", False)
    cids = [o[0] for o in observations]
    frozen = breaker_active(conn)
    results = []
    if not frozen and cids and breaker_evaluate(conn, cids):
        breaker_freeze(conn, "blast-radius", "scan")
        frozen = True
    for idx, obs in enumerate(observations):
        cid, code, body = obs
        if frozen and not shadow and idx > 0:
            # batch yang MEMICU freeze (idx 0) tetap diproses; sisanya beku
            ks = key_row(conn, cid)
            explicit, reason = classify_error(code, body)
            record_event(conn, "breaker_skipped", cid, ks["provider_node_id"] if ks else None,
                         json.dumps({"code": code, "reason": reason, "explicit": explicit}))
            results.append({"connection_id": cid, "action": "frozen-skip"})
            continue
        results.append({**observe_error(conn, cid, code, body, shadow), "connection_id": cid})
    return results

def canary_recover(conn, connection_id, ok, shadow=None):
    """Hasil canary exact-key. ok=True -> Recovering + lease. False -> tetap/naik backoff."""
    if shadow is None:
        st = get_engine(conn, ENG_HEALTH)
        shadow = not st.get("enabled", False)
    ks = key_row(conn, connection_id)
    if ks is None:
        return {"action": "unknown-connection"}
    if ks["desired"] == "Disabled":
        return {"action": "manual-disabled"}
    if not ok:
        if shadow:
            record_event(conn, "shadow_canary_fail", connection_id, ks["provider_node_id"], "")
            return {"action": "shadow-canary-fail"}
        conn.execute(
            """UPDATE rkm_key_state SET health='Unhealthy', retry_at=?, failure_streak=failure_streak+1,
               recovery_gen=recovery_gen+1, lease_expires=NULL, lease_budget=0, updated_at=?
               WHERE connection_id=?""",
            (next_retry(ks["failure_streak"] + 1), now_iso(), connection_id))
        conn.commit()
        record_event(conn, "canary_fail", connection_id, ks["provider_node_id"], "")
        return {"action": "canary-fail"}
    if shadow:
        record_event(conn, "shadow_canary_ok", connection_id, ks["provider_node_id"], "")
        return {"action": "shadow-recovering"}
    lease_exp = iso_add(now_iso(), LEASE_SEC)
    conn.execute(
        """UPDATE rkm_key_state SET health='Recovering', health_reason=NULL, retry_at=NULL,
           lease_expires=?, lease_budget=?, updated_at=?, updated_by='engine'
           WHERE connection_id=?""",
        (lease_exp, LEASE_BUDGET, now_iso(), connection_id))
    conn.execute("UPDATE providerConnections SET isActive=1, updatedAt=? WHERE id=?", (now_iso(), connection_id))
    conn.commit()
    record_event(conn, "canary_ok_recovering", connection_id, ks["provider_node_id"], "")
    return {"action": "recovering", "lease_expires": lease_exp}

def observe_production_success(conn, connection_id, shadow=None):
    """Sukses produksi nyata. Syarat: connectionId sama + generation tidak lebih baru
    (sukses stale tidak boleh menyembuhkan). Hanya ini yang menghapus streak."""
    if shadow is None:
        st = get_engine(conn, ENG_HEALTH)
        shadow = not st.get("enabled", False)
    ks = key_row(conn, connection_id)
    if ks is None:
        return {"action": "unknown-connection"}
    if ks["desired"] == "Disabled":
        return {"action": "manual-disabled"}
    if shadow:
        record_event(conn, "shadow_prod_success", connection_id, ks["provider_node_id"], "")
        return {"action": "shadow-healthy"}
    conn.execute(
        """UPDATE rkm_key_state SET health='Healthy', health_reason=NULL, retry_at=NULL,
           failure_streak=0, lease_expires=NULL, lease_budget=0, updated_at=?
           WHERE connection_id=?""",
        (now_iso(), connection_id))
    conn.commit()
    record_event(conn, "key_healthy", connection_id, ks["provider_node_id"], "")
    return {"action": "healthy"}

def expire_leases(conn, shadow=None):
    """Lease Recovering kedaluwarsa -> kembali Disabled routing + backoff."""
    if shadow is None:
        st = get_engine(conn, ENG_HEALTH)
        shadow = not st.get("enabled", False)
    ts = now_iso()
    rows = conn.execute(
        "SELECT connection_id, provider_node_id, failure_streak FROM rkm_key_state "
        "WHERE health='Recovering' AND lease_expires IS NOT NULL AND lease_expires < ?",
        (ts,)).fetchall()
    out = []
    for r in rows:
        if not shadow:
            conn.execute(
                """UPDATE rkm_key_state SET health='Unhealthy', health_reason='Unknown',
                   retry_at=?, recovery_gen=recovery_gen+1,
                   lease_expires=NULL, lease_budget=0, updated_at=? WHERE connection_id=?""",
                (iso_add(ts, LEASE_SEC), ts, r["connection_id"]))
            conn.execute("UPDATE providerConnections SET isActive=0, updatedAt=? WHERE id=?",
                         (ts, r["connection_id"]))
            conn.commit()
        record_event(conn, "lease_expired", r["connection_id"], r["provider_node_id"], "")
        out.append(r["connection_id"])
    return out

def due_retries(conn):
    """Kandidat canary: Unhealthy + retryAt lewat + Desired Enabled + tidak dalam incident."""
    ts = now_iso()
    return conn.execute(
        "SELECT connection_id, provider_node_id, failure_streak, retry_at FROM rkm_key_state "
        "WHERE desired='Enabled' AND health='Unhealthy' AND (retry_at IS NULL OR retry_at <= ?) "
        "AND provider_node_id NOT IN (SELECT incident_domain FROM rkm_provider_incident WHERE status='Open')",
        (ts,)).fetchall()

def provider_incident_open(conn, provider_node_id):
    return conn.execute(
        "SELECT id FROM rkm_provider_incident WHERE incident_domain=? AND status != 'Closed'",
        (provider_node_id,)).fetchone() is not None

def open_provider_incident(conn, provider_node_id, reason=""):
    conn.execute(
        "INSERT INTO rkm_provider_incident(incident_domain,status,reason,opened_at) VALUES(?,?,?,?)",
        (provider_node_id, "Open", reason, now_iso()))
    conn.commit()
    record_event(conn, "incident_open", None, provider_node_id, reason)

def close_provider_incident(conn, provider_node_id, status="Closed"):
    conn.execute(
        "UPDATE rkm_provider_incident SET status=?, closed_at=? WHERE incident_domain=? AND status!='Closed'",
        (status, now_iso(), provider_node_id))
    conn.commit()
    record_event(conn, "incident_close", None, provider_node_id, status)

# ---------- combo eligibility ----------

def eligible_providers(conn):
    """Provider yang boleh menyumbang model combo: >=1 key Desired+Routing Enabled & Healthy,
    dan tidak dalam Provider Incident aktif."""
    rows = conn.execute(
        "SELECT DISTINCT provider_node_id FROM rkm_key_state "
        "WHERE desired='Enabled' AND routing='Enabled' AND health='Healthy'").fetchall()
    return {r["provider_node_id"] for r in rows
            if not provider_incident_open(conn, r["provider_node_id"])}

def snapshot(conn):
    """Ringkasan status untuk UI / shadow laporan."""
    agg = conn.execute(
        "SELECT desired, routing, health, COUNT(*) c FROM rkm_key_state GROUP BY desired, routing, health").fetchall()
    incidents = conn.execute(
        "SELECT incident_domain, status, reason FROM rkm_provider_incident WHERE status!='Closed'").fetchall()
    st = get_engine(conn, ENG_HEALTH)
    return {
        "ts": now_iso(),
        "engine_health_enabled": st.get("enabled", False),
        "freeze": st.get("freeze", {}),
        "aggregates": [{"desired": a["desired"], "routing": a["routing"], "health": a["health"], "count": a["c"]} for a in agg],
        "incidents": [{"domain": i["incident_domain"], "status": i["status"], "reason": i["reason"]} for i in incidents],
        "shadow_events_24h": conn.execute(
            "SELECT kind, COUNT(*) c FROM rkm_event WHERE ts > ? GROUP BY kind",
            (iso_add(now_iso(), -86400),)).fetchall().__len__() and {
                r["kind"]: r["c"] for r in conn.execute(
                    "SELECT kind, COUNT(*) c FROM rkm_event WHERE ts > ? GROUP BY kind",
                    (iso_add(now_iso(), -86400),)).fetchall()},
    }

def selfcheck(db_path=None):
    """Smoke test state-machine pada file temp. EXIT 0 = PASS."""
    import tempfile
    path = db_path or os.path.join(tempfile.gettempdir(), "rkm_selfcheck.sqlite")
    if os.path.exists(path):
        os.remove(path)
    conn = open_db(path)
    ensure_schema(conn)
    conn.execute("CREATE TABLE providerConnections (id TEXT PRIMARY KEY, provider TEXT, authType TEXT, name TEXT, email TEXT, priority INTEGER, isActive INTEGER DEFAULT 1, data TEXT NOT NULL, createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL)")
    assert bootstrap(conn) == 0
    # 2 key, 2 provider
    conn.execute("INSERT INTO providerConnections(id,provider,authType,name,isActive,data,createdAt,updatedAt) VALUES('k1','provA','key','a',1,?,?,'x')", (json.dumps({"apiKey": "AK1"}), now_iso()))
    conn.execute("INSERT INTO providerConnections(id,provider,authType,name,isActive,data,createdAt,updatedAt) VALUES('k2','provB','key','b',1,?,?,'x')", (json.dumps({"apiKey": "AK2"}), now_iso()))
    conn.commit()
    assert bootstrap(conn) == 2
    # shadow: eksplisit tidak mematikan
    r = observe_error(conn, "k1", 401, "invalid api key")
    assert r["action"] == "shadow-unhealthy", r
    assert conn.execute("SELECT isActive FROM providerConnections WHERE id='k1'").fetchone()[0] == 1
    # enforce ON
    set_engine(conn, ENG_HEALTH, {"enabled": True, "by": "selfcheck"})
    r = observe_error(conn, "k1", 401, "invalid api key")
    assert r["action"] == "unhealthy", r
    assert conn.execute("SELECT isActive FROM providerConnections WHERE id='k1'").fetchone()[0] == 0
    ks = key_row(conn, "k1")
    assert ks["health"] == "Unhealthy" and ks["health_reason"] == "Auth" and ks["retry_at"]
    # ambigu tidak mematikan
    r = observe_error(conn, "k2", 503, "model overloaded")
    assert r["action"] == "observe-only", r
    assert conn.execute("SELECT isActive FROM providerConnections WHERE id='k2'").fetchone()[0] == 1
    # canary ok -> recovering + routing on + lease
    r = canary_recover(conn, "k1", True)
    assert r["action"] == "recovering"
    assert conn.execute("SELECT isActive FROM providerConnections WHERE id='k1'").fetchone()[0] == 1
    ks = key_row(conn, "k1")
    assert ks["health"] == "Recovering" and ks["lease_budget"] == LEASE_BUDGET and ks["lease_expires"]
    # sukses produksi -> healthy, streak 0
    r = observe_production_success(conn, "k1")
    assert r["action"] == "healthy"
    ks = key_row(conn, "k1")
    assert ks["health"] == "Healthy" and ks["failure_streak"] == 0
    # manual disabled menang
    conn.execute("UPDATE rkm_key_state SET desired='Disabled' WHERE connection_id='k2'")
    conn.commit()
    r = observe_error(conn, "k2", 401, "invalid api key")
    assert r["action"] == "manual-disabled", r
    assert conn.execute("SELECT isActive FROM providerConnections WHERE id='k2'").fetchone()[0] == 1
    # breaker: batch besar dibekukan
    for i in range(3, 12):
        conn.execute("INSERT INTO providerConnections(id,provider,authType,name,isActive,data,createdAt,updatedAt) VALUES(?,?,?,?,1,?,?,'x')",
                     (f"k{i}", "provA" if i < 7 else "provB", "key", f"n{i}", json.dumps({"apiKey": f"AK{i}"}), now_iso()))
    conn.commit()
    assert bootstrap(conn) == 11
    obs = [(f"k{i}", 401, "invalid api key") for i in (3, 4, 5, 6, 7, 8, 9, 10, 11) if i <= 10]
    res = observe_batch(conn, obs)
    acts = {x.get("action") for x in res}
    assert "unhealthy" in acts and "frozen-skip" in acts, acts
    assert breaker_active(conn)
    # manual disable tetap boleh saat freeze (desired toggle, bukan health mutasi)
    # lease expire path
    conn.execute("UPDATE rkm_key_state SET lease_expires='2000-01-01T00:00:00.000Z', health='Recovering' WHERE connection_id='k1'")
    conn.commit()
    expired = expire_leases(conn)
    assert "k1" in expired, expired
    assert conn.execute("SELECT isActive FROM providerConnections WHERE id='k1'").fetchone()[0] == 0
    # due_retries & incident gating
    conn.execute("UPDATE rkm_key_state SET retry_at='2000-01-01T00:00:00.000Z' WHERE connection_id='k1'")
    conn.commit()
    due = [r["connection_id"] for r in due_retries(conn)]
    assert "k1" in due, due
    open_provider_incident(conn, "provA", "503 massal")
    eligible = eligible_providers(conn)
    assert "provA" not in eligible, eligible
    close_provider_incident(conn, "provA")
    # classifier sanity
    assert classify_error(401, "The bound service account is deleted") == (True, "Auth")
    assert classify_error(402, "insufficient credits") == (True, "Billing")
    assert classify_error(429, "rate limited") == (False, "Unknown")
    assert classify_error(404, "model does not exist") == (False, "Unknown")
    assert classify_error(503, "fetch connect timeout") == (False, "Provider")
    assert classify_error(403, "Your project has been denied access") == (True, "Auth")
    conn.close()
    os.remove(path)
    print("rkm_state selfcheck PASS")
    return 0

if __name__ == "__main__":
    import sys
    if "--selfcheck" in sys.argv:
        sys.exit(selfcheck())
    if "--bootstrap" in sys.argv:
        conn = open_db()
        ensure_schema(conn)
        n = bootstrap(conn)
        log(f"bootstrap {n} koneksi; schema ok")
        conn.close()
        sys.exit(0)
    if "--snapshot" in sys.argv:
        conn = open_db()
        ensure_schema(conn)
        print(json.dumps(snapshot(conn), indent=2, default=str))
        conn.close()
        sys.exit(0)
    print("usage: rkm_state.py --selfcheck|--bootstrap|--snapshot")
    sys.exit(1)
