#!/usr/bin/env python3
"""
rkm_canary.py - 9RKM v2 Canary Registry & exact-key probe (2026-09-01)

Canary = uji kredensial key TERTENTU langsung ke endpoint provider,
bukan lewat gateway (gateway tidak menjamin key mana yang dipakai).

Registry per provider (rkm_canary_registry di rkm_engine_state):
  {"<provider_node_id>": {"kind": "models", "url": "...", "apiKey": "<key>"}}

Kebijakan (ADR 0004/0005):
- Endpoint account (GET /models, /v1/me, /api/credit) diutamakan: murah,
  tanpa inference, membuktikan kredensial hidup.
- Fallback inference generik hanya bila endpoint account tidak ada:
  1 request max_tokens minimal ke model paling murah.
- TANPA registry tepercaya = Unsupported Canary (key tidak boleh
  Recovering otomatis; hanya routing terbatas manual Dea).
- Hasil canary HANYA menulis rkm_key_state via rkm_state.canary_recover()
  (yang di shadow mode hanya mencatat event, tanpa mutasi).

CLI:
  --register <provider_node_id>   auto-build registry dari connection aktif
  --register-all                  semua provider yang punya baseUrl+apiKey
  --probe <connection_id>         probe satu key, tulis hasil via state machine
  --probe-due                     semua Unhealthy yang retryAt sudah lewat
  --list                          tampilkan registry
  --report                        ringkasan health semua key
"""
import os
import sys
import json
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rkm_state as R

DB = os.environ.get("ROUTER_DB", "/home/ubuntu/.9router/db/data.sqlite")
REGISTRY_KEY = "canary_registry"
PROBE_TIMEOUT = int(os.environ.get("RKM_CANARY_TIMEOUT", "15"))

# endpoint account per tipe node (coba berurutan, pakai yang pertama 2xx)
ACCOUNT_PATHS = ("/v1/models", "/models", "/v1/me", "/api/v1/me", "/api/credit", "/api/v1/chat/credit")


def log(msg):
    R.log(f"[canary] {msg}")


# ---------- registry ----------

def load_registry(conn):
    st = R.get_engine(conn, R.ENG_HEALTH)
    return st.get(REGISTRY_KEY, {})


def save_registry(conn, registry):
    st = R.get_engine(conn, R.ENG_HEALTH)
    st[REGISTRY_KEY] = registry
    R.set_engine(conn, R.ENG_HEALTH, st, by="canary")


def build_entry_for_connection(row):
    """Auto-build canary entry dari satu providerConnection. None = Unsupported."""
    try:
        data = json.loads(row["data"]) if row["data"] else {}
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    psd = data.get("providerSpecificData") or {}
    base = (psd.get("baseUrl") or "").rstrip("/")
    key = data.get("apiKey") or ""
    if not base or not key:
        return None
    # ponytail: kind chat = inference minimal (occ/ocr blokir /models via WAF/404);
    # kind models = endpoint account murah. Model canary = model termurah umum,
    # tidak dipakai menilai model — hanya membuktikan kredensial diterima.
    kind = "chat" if "opencode.ai" in base else "models"
    return {"kind": kind, "url": base, "apiKey": key, "canary_model": psd.get("prefixCanaryModel") or "muse-spark-1.2-contributor-free"}


def register(conn, provider_node_id, active_only=True):
    """Registry entry per PROVIDER: pakai key connection aktif pertama
    sebagai kredensial PENGUJI endpoint (bukan key yang diuji — key yang
    diuji selalu key milik connection target, lihat probe())."""
    sql = "SELECT id, provider, isActive, data FROM providerConnections WHERE provider=?"
    if active_only:
        sql += " AND isActive=1"
    row = conn.execute(sql + " LIMIT 1", (provider_node_id,)).fetchone()
    if not row:
        return False
    entry = build_entry_for_connection(row)
    if not entry:
        return False
    reg = load_registry(conn)
    reg[provider_node_id] = entry
    save_registry(conn, reg)
    return True


def register_all(conn):
    rows = conn.execute("SELECT DISTINCT provider FROM providerConnections").fetchall()
    ok, skip = [], []
    for r in rows:
        p = r["provider"]
        if register(conn, p):
            ok.append(p)
        else:
            skip.append(p)
    return ok, skip


# ---------- probe ----------

BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"


def _http_get(url, api_key):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "User-Agent": BROWSER_UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            resp.read(2048)
            return 200 <= resp.status < 300, resp.status, ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            body = str(e)
        return False, e.code, body
    except Exception as e:
        return False, 0, str(e)


def _http_chat(url, api_key, model):
    """Inference minimal — membuktikan kredensial diterima (4xx auth/billing =
    bukti eksplisit; 5xx = inconclusive/infra, bukan bukti key mati)."""
    payload = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": 16, "stream": False}).encode()
    req = urllib.request.Request(url + "/chat/completions", data=payload, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": BROWSER_UA,
    })
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as resp:
            resp.read(2048)
            return 200 <= resp.status < 300, resp.status, ""
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            body = str(e)
        return False, e.code, body
    except Exception as e:
        return False, 0, str(e)


def canary_key(conn, connection_id, entry=None):
    """Probe EXACT-KEY: kredensial yang diuji = apiKey connection target.
    Entry registry hanya menyediakan endpoint/baseUrl; key di-override
    dengan key milik connection target."""
    row = conn.execute("SELECT provider, data FROM providerConnections WHERE id=?", (connection_id,)).fetchone()
    if not row:
        return False, "no-connection"
    try:
        data = json.loads(row["data"]) if row["data"] else {}
    except Exception:
        data = {}
    target_key = data.get("apiKey") or ""
    prov = row["provider"]
    if entry is None:
        entry = load_registry(conn).get(prov)
    if not entry or not target_key:
        return False, "unsupported-canary"
    if entry.get("kind") == "chat":
        ok, status, body = _http_chat(entry["url"], target_key, entry.get("canary_model") or "gpt-3.5-turbo")
        if not ok and status >= 500:
            return False, f"inconclusive http {status} (infra, bukan bukti key)"  # 5xx jangan dihitung bukti mati
    else:
        ok, status, body = _http_get(entry["url"] + "/models" if not entry["url"].endswith("/models") else entry["url"], target_key)
    return ok, f"http {status} {body[:120]}" if not ok else f"2xx {entry['url']}"


def probe_and_apply(conn, connection_id, shadow=None):
    """Probe satu key lalu terapkan hasil lewat state machine.
    Di shadow: hanya event. Di enforce: Recovering/Unhealthy + routing."""
    if shadow is None:
        shadow = not R.get_engine(conn, R.ENG_HEALTH).get("enabled", False)
    ok, detail = canary_key(conn, connection_id)
    result = R.canary_recover(conn, connection_id, ok, shadow=shadow)
    log(f"probe {connection_id}: {'OK' if ok else 'FAIL'} ({detail}) -> {result['action']}")
    return {**result, "probe_ok": ok, "probe_detail": detail}


def probe_due(conn, limit=50, shadow=None):
    out = []
    for r in R.due_retries(conn):
        if len(out) >= limit:
            break
        out.append(probe_and_apply(conn, r["connection_id"], shadow=shadow))
    return out


# ---------- report ----------

def report(conn):
    rows = conn.execute(
        "SELECT k.connection_id, k.provider_node_id p, k.health, k.health_reason, "
        "k.failure_streak, k.retry_at, k.desired, k.routing, c.isActive "
        "FROM rkm_key_state k LEFT JOIN providerConnections c ON c.id = k.connection_id "
        "ORDER BY k.health, k.provider_node_id").fetchall()
    reg = load_registry(conn)
    return {
        "ts": R.now_iso(),
        "registry_providers": sorted(reg.keys()),
        "keys": [dict(r) for r in rows],
    }


def selfcheck():
    import tempfile, sqlite3
    path = os.path.join(tempfile.gettempdir(), "rkm_canary_selfcheck.sqlite")
    if os.path.exists(path):
        os.remove(path)
    conn = R.open_db(path)
    R.ensure_schema(conn)
    conn.execute("CREATE TABLE providerConnections (id TEXT PRIMARY KEY, provider TEXT, isActive INTEGER, data TEXT, updatedAt TEXT)")
    conn.execute("INSERT INTO providerConnections VALUES('k1','provX',1,?,?)",
                 (json.dumps({"apiKey": "KEY1", "providerSpecificData": {"baseUrl": "http://example.invalid/v1"}}), R.now_iso()))
    conn.execute("INSERT INTO providerConnections VALUES('k2','provX',1,?,?)",
                 (json.dumps({"apiKey": "KEY2", "providerSpecificData": {"baseUrl": "http://example.invalid/v1"}}), R.now_iso()))
    conn.commit()
    assert R.bootstrap(conn) == 2
    assert register(conn, "provX"), "register harus sukses dari baseUrl+apiKey"
    reg = load_registry(conn)
    assert "provX" in reg and reg["provX"]["url"].endswith("/v1")
    assert reg["provX"]["kind"] == "models", "non-opencode.ai default kind=models"
    # exact-key: kredensial target menang atas registry
    entry = reg["provX"]
    conn.execute("INSERT INTO providerConnections VALUES('k9','provX',1,?,?)",
                 (json.dumps({"apiKey": "KEY9", "providerSpecificData": {"baseUrl": "http://example.invalid/v1"}}), R.now_iso()))
    conn.commit()
    R.ensure_schema(conn)
    R.bootstrap(conn)
    ok, detail = canary_key(conn, "k9", entry=entry)
    assert ok is False and "FAIL" != ok, detail  # example.invalid tidak mungkin 2xx
    assert "unsupported-canary" not in detail
    # key tanpa apiKey -> unsupported
    conn.execute("INSERT INTO providerConnections VALUES('k10','provY',1,?,?)",
                 (json.dumps({"providerSpecificData": {"baseUrl": "http://x.example"}}), R.now_iso()))
    conn.commit()
    R.bootstrap(conn)
    ok2, detail2 = canary_key(conn, "k10")
    assert detail2 == "unsupported-canary", detail2
    # probe_due di shadow tidak memutasi routing
    R.set_engine(conn, R.ENG_HEALTH, {**R.get_engine(conn, R.ENG_HEALTH), "enabled": False})
    conn.execute("UPDATE rkm_key_state SET health='Unhealthy', retry_at='2000-01-01T00:00:00.000Z' WHERE connection_id='k1'")
    conn.commit()
    # k1 kini HARUS jalur HTTP nyata (registry masih ada setelah merge)
    ok_http, det_http = canary_key(conn, "k1")
    assert ok_http is False and det_http.startswith("http 0") or "HTTP" in str(det_http).upper() or det_http, det_http
    assert det_http != "unsupported-canary", "registry terhapus oleh set_engine — wajib merge"
    res = probe_due(conn, shadow=True)
    assert res and all(x["action"].startswith("shadow") or x["action"] == "manual-disabled" or x["action"] == "unknown-connection" for x in res), res
    act = conn.execute("SELECT isActive FROM providerConnections WHERE id='k1'").fetchone()[0]
    assert act == 1, "shadow canary tidak boleh mematikan"
    conn.close()
    os.remove(path)
    print("rkm_canary selfcheck PASS")
    return 0


def main():
    args = sys.argv[1:]
    if "--selfcheck" in args:
        sys.exit(selfcheck())
    conn = R.open_db(DB)
    R.ensure_schema(conn)
    if "--register-all" in args:
        ok, skip = register_all(conn)
        log(f"register ok={len(ok)} unsupported={len(skip)}: {sorted(ok)}")
        print(json.dumps({"registered": sorted(ok), "unsupported": sorted(skip)}, indent=1))
        return
    if "--register" in args:
        p = args[args.index("--register") + 1]
        print(json.dumps({"ok": register(conn, p)}))
        return
    if "--list" in args:
        reg = load_registry(conn)
        safe = {p: {"kind": e.get("kind"), "url": e.get("url")} for p, e in reg.items()}
        print(json.dumps(safe, indent=1))
        return
    if "--probe" in args:
        cid = args[args.index("--probe") + 1]
        print(json.dumps(probe_and_apply(conn, cid), indent=1))
        return
    if "--probe-due" in args:
        for r in probe_due(conn):
            print(json.dumps(r))
        return
    if "--report" in args:
        rep = report(conn)
        agg = {}
        for k in rep["keys"]:
            agg[k["health"]] = agg.get(k["health"], 0) + 1
        print(json.dumps({"ts": rep["ts"], "registry_providers": rep["registry_providers"], "health_agg": agg}, indent=1))
        return
    print(__doc__)
    conn.close()


if __name__ == "__main__":
    main()
