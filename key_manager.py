#!/usr/bin/env python3
"""
9RKM - 9Router Key Manager, edisi remap-only (2026-09-05, amendemen PRD remap-only).
Satu daemon remap combo model terbaik tiap siklus 5 jam + auto-remap saat versi
AA Intelligence Index berubah:
  - Thread scheduler: remap terjadwal 5 jam + cek versi ringan 1x/jam
  - Thread HTTP      : Web UI status + REMAP manual + approve alias, Tailscale-only
On/off key DIHAPUS TOTAL (scan 5s, reset, bulk, toggle): 9RKM tidak pernah menulis
providerConnections.isActive. Saringan satu-satunya = probe 2xx saat remap.
Arsip versi on/off: riwayat git repo ini (pre-remap-only) + backup deploy §12.7.
"""
import sys, os, time, json, sqlite3, datetime, threading
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
CYCLE_HOURS = 5
CYCLE_SECONDS = CYCLE_HOURS * 3600
SCHED_TICK_SEC = 30
SLEEP_INTERVAL = SCHED_TICK_SEC
REMAP_LOCK = "/tmp/9rkm-remap.lock"
REMAP_DEBOUNCE_SEC = 1800
REMAP_STALE_SEC = 1800
REMAP_LOG = "/tmp/aa_remap_last.log"
REMAP_CYCLE_SCOPE = "aa_remap_cycle"
REMAP_CYCLE_KEY = "state"
VERSION_SCOPE = "aa_version"
VERSION_KEY = "state"
VERSION_CHECK_SEC = 3600
VERSION_MIN_INTERVAL_SEC = 7200
AA_API_BASE = "https://artificialanalysis.ai/api/v2/language/models/free"
AA_KEY_ENV = "AA_API_KEY"
COMBO_NAMES = ("Artificial-Analysis-Intelligence-Index",)
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
    try:
        print(f"{ts} | {msg}", flush=True)
    except UnicodeEncodeError:
        safe = f"{ts} | {msg}".encode("ascii", "replace").decode("ascii")
        print(safe, flush=True)

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
    out = {"lastAt": None, "lastWib": None, "source": None, "intel": None, "coverage": None, "vision": None, "ver": None, "cacheAt": None, "cacheAgeH": None, "locked": False, "cooldownSec": 0}
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
            out["vision"] = j.get("vision")
            out["ver"] = j.get("ver")
            out["coverage"] = j.get("coverage")
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

def _router_api_key():
    conn = get_db()
    try:
        row = conn.execute("SELECT key FROM apiKeys LIMIT 1").fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _stream_ok(raw):
    # SSE stream: first content-bearing data chunk decides (responses-kind
    # models return empty body with stream:false, so stream:true is required).
    if "data:" in raw:
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            if "error" in data and "delta" not in data and "choices" not in data:
                return False
            return True
        return False
    return '"choices"' in raw and '"error"' not in raw[:500]


def _probe_combo(name):
    api_key = _router_api_key()
    if not api_key:
        return False
    payload = json.dumps({"model": name, "messages": [{"role": "user", "content": "Reply PONG only"}], "max_tokens": 64, "stream": True}).encode()
    request = urllib.request.Request("http://127.0.0.1:20128/v1/chat/completions", data=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            if not (200 <= response.status < 300):
                return False
            raw = response.read().decode("utf-8", errors="replace")
    # NOTE: timeout 600s — combo 17 fallback berurutan butuh waktu; E2E boleh lambat asal 2xx+konten.
    except Exception:
        return False
    return _stream_ok(raw)


def _mark_remap_rolled_back(reason):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM kv WHERE scope=? AND key=?", ("aa_remap", "state"))
        row = cur.fetchone()
        prev = {}
        if row and row[0]:
            try:
                prev = json.loads(row[0])
            except Exception:
                prev = {}
        n = 0
        for name in COMBO_NAMES:
            r = cur.execute("SELECT models FROM combos WHERE name=?", (name,)).fetchone()
            if r and r[0]:
                try:
                    arr = json.loads(r[0])
                    if isinstance(arr, list):
                        n = len(arr)
                except Exception:
                    pass
        state = {"at": get_iso_now(), "source": "rollback:" + str(reason)[:60],
                 "intel": n, "coverage": {"topk": 0, "covered": 0, "pct": 0.0, "warn": True, "missing": []},
                 "vision": prev.get("vision"), "ver": prev.get("ver"),
                 "backup": prev.get("backup"), "rollback": True}
        cur.execute("INSERT INTO kv(scope,key,value) VALUES(?,?,?) ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value",
                    ("aa_remap", "state", json.dumps(state)))
        conn.commit()
    except Exception as e:
        log(f"[Remap] tandai rollback gagal {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _combo_first_model():
    conn = get_db()
    try:
        for name in COMBO_NAMES:
            row = conn.execute("SELECT models FROM combos WHERE name=?", (name,)).fetchone()
            if row and row[0]:
                try:
                    arr = json.loads(row[0])
                    if isinstance(arr, list) and arr:
                        return arr[0]
                except Exception:
                    pass
    finally:
        conn.close()
    return None


def _probe_model_direct(mid, timeout=180):
    api_key = _router_api_key()
    if not api_key:
        return False
    payload = json.dumps({"model": mid, "messages": [{"role": "user", "content": "Reply PONG only"}], "max_tokens": 64, "stream": True}).encode()
    request = urllib.request.Request("http://127.0.0.1:20128/v1/chat/completions", data=payload, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not (200 <= response.status < 300):
                return False
            raw = response.read().decode("utf-8", errors="replace")
    except Exception:
        return False
    return _stream_ok(raw)

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

# ---------- Version watcher (FR-8) ----------

def _aa_api_key():
    if os.environ.get(AA_KEY_ENV):
        return os.environ[AA_KEY_ENV].strip()
    for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), ".aa_api_key"),
              os.path.expanduser("~/scripts/9rkm/.aa_api_key"),
              "/home/ubuntu/scripts/9rkm/.aa_api_key"):
        try:
            if os.path.exists(p):
                v = pathlib.Path(p).read_text(encoding="utf-8").strip()
                if v:
                    return v
        except Exception:
            pass
    return ""

def _read_version_state():
    conn = get_db()
    try:
        return load_state_from_db(conn.cursor(), VERSION_SCOPE, VERSION_KEY)
    finally:
        conn.close()

def _save_version_state(state):
    conn = get_db()
    try:
        save_state_to_db(conn.cursor(), state, VERSION_SCOPE, VERSION_KEY)
        conn.commit()
    finally:
        conn.close()

def _fetch_aa_version(timeout=25):
    """Cek ringan: page=1 saja, baca intelligence_index_version. Hemat kuota."""
    key = _aa_api_key()
    if not key:
        raise RuntimeError("AA API key missing")
    req = urllib.request.Request(f"{AA_API_BASE}?page=1",
                                 headers={"x-api-key": key, "User-Agent": "9rkm-version-watch/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode())
    return body.get("intelligence_index_version")

def _remap_ver():
    """Versi AA saat remap terakhir (ditulis aa_rank ke kv aa_remap/state)."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT value FROM kv WHERE scope=? AND key=?", ("aa_remap", "state"))
        row = cur.fetchone()
        if row and row["value"]:
            try:
                return json.loads(row["value"]).get("ver")
            except Exception:
                pass
    finally:
        conn.close()
    return None

def _check_version_changed():
    """True bila versi live != versi remap terakhir. Selalu catat hasil cek."""
    now = time.time()
    vst = _read_version_state()
    try:
        live = _fetch_aa_version()
    except Exception as e:
        log(f"[Version] cek gagal: {e}")
        vst["lastCheckAt"] = get_iso_now()
        vst["lastCheckError"] = str(e)[:120]
        _save_version_state(vst)
        return False, None, None
    prev_seen = vst.get("ver")
    remap_ver = _remap_ver()
    vst.update({"ver": live, "prevVer": prev_seen, "lastCheckAt": get_iso_now()})
    vst.pop("lastCheckError", None)
    _save_version_state(vst)
    base = remap_ver or prev_seen
    if live and base and live != base:
        last_trig = 0
        try:
            last_trig = float(_cycle_state().get("lastVersionTrig") or 0)
        except Exception:
            last_trig = 0
        if now - last_trig < VERSION_MIN_INTERVAL_SEC:
            log(f"[Version] {base} -> {live} (cooldown, remap ditunda)")
            return False, live, base
        return True, live, base
    return False, live, base

def _due_schedule():
    return _cycle_state().get("successCycle") != get_cycle_id()

def remap_scheduler_thread():
    fails = 0
    last_ver_check = 0
    while True:
        try:
            if _due_schedule():
                code = _run_remap(reason="schedule")
                last_ver_check = 0
            else:
                now = time.time()
                if now - last_ver_check >= VERSION_CHECK_SEC:
                    last_ver_check = now
                    changed, live, base = _check_version_changed()
                    if changed:
                        log(f"[Version] {base} -> {live}: remap segera")
                        code = _run_remap(force=True, reason=f"version:{base}->{live}")
                        if code == 0:
                            _save_cycle_state({**_cycle_state(), "lastVersionTrig": now})
                        else:
                            fails += 1
                            if fails == 3:
                                notify("Remap gagal 3x beruntun — backoff eksponensial aktif, cek coverage/E2E di /api/status.")
                            time.sleep(min(300 * (2 ** min(fails - 1, 3)), 3600))
                            continue
                        last_ver_check = 0
                fails = 0
                time.sleep(SCHED_TICK_SEC)
                continue
            if code == 5:
                time.sleep(30)
            elif code:
                fails += 1
                if fails == 3:
                    notify("Remap gagal 3x beruntun — backoff eksponensial aktif, cek coverage/E2E di /api/status.")
                time.sleep(min(300 * (2 ** min(fails - 1, 3)), 3600))
            else:
                fails = 0
                time.sleep(SCHED_TICK_SEC)
        except Exception as e:
            log(f"[-] Scheduler error: {e}")
            time.sleep(SCHED_TICK_SEC)

def _restart_router():
    result = subprocess.run(["pm2", "restart", "9router"], capture_output=True, text=True, timeout=30)
    return result.returncode == 0

def _wait_router_ready(timeout=180):
    """Poll /api/health sampai 200 — 9Router butuh waktu boot sesudah restart;
    tanpa ini E2E menembak server yang belum siap (false-fail + rollback sia-sia)."""
    import urllib.request
    deadline = time.time() + timeout
    last = "n/a"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:20128/api/health", timeout=10) as r:
                if 200 <= r.status < 300:
                    return True
                last = f"http={r.status}"
        except Exception as e:
            last = f"{type(e).__name__}"
        time.sleep(5)
    log(f"[Remap] router not ready after {timeout}s (last={last})")
    return False

def _run_remap(force=False, reason="schedule"):
    cycle_id = get_cycle_id()
    if not force and _cycle_state().get("successCycle") == cycle_id and reason == "schedule":
        return 0
    descriptor = _acquire_remap_lock()
    if descriptor is None:
        log("[Remap] locked skip")
        return 5
    snapshot = None
    output = ""
    try:
        snapshot = _combo_snapshot()
        log("[Remap] start discovery cycle")
        child_env = {**os.environ, "AA_REMAP_LOCK_HELD": "1"}
        result = subprocess.run(
            ["/usr/bin/python3", "/home/ubuntu/scripts/9rkm/aa_rank.py", "--remap", "--no-vision"],
            capture_output=True,
            text=True,
            timeout=1800,
            env=child_env,
        )
        output = (result.stdout or "")[-16000:] + (result.stderr or "")[-4000:]
        if result.returncode != 0:
            raise RuntimeError(f"aa_rank exit {result.returncode}")
        if not _restart_router():
            raise RuntimeError("pm2 restart failed")
        if not _wait_router_ready():
            raise RuntimeError("router not ready after restart")
        if not all(_probe_combo(name) for name in COMBO_NAMES):
            raise RuntimeError("combo E2E failed")
        first = _combo_first_model()
        if not first or not _probe_model_direct(first):
            raise RuntimeError("first-model E2E failed")
        _save_cycle_state({"successCycle": cycle_id, "at": get_iso_now(), "status": "ok",
                           "reason": reason, "lastReasonAt": get_iso_now()})
        log(f"[Remap] verified E2E ({reason})")
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
        _mark_remap_rolled_back(error)
        _save_cycle_state({"successCycle": _cycle_state().get("successCycle"), "attemptCycle": cycle_id, "at": get_iso_now(), "status": f"error:{error}"})
        return 4
    finally:
        try:
            if output:
                pathlib.Path(REMAP_LOG).write_text(output, encoding="utf-8")
        except Exception as log_error:
                log(f"[Remap] tulis log gagal {log_error}")
        _release_remap_lock(descriptor)

def _run_remap_async(force=False, reason="manual"):
    def worker():
        code = _run_remap(True, reason)
        notify(f"Remap combo selesai force={force} reason={reason} exit={code} @ {get_iso_now()}")
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

def _state_row_active(db_path, cid):
    """Helper test/inspeksi: isActive satu koneksi."""
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT isActive FROM providerConnections WHERE id=?", (cid,)).fetchone()[0]
    finally:
        conn.close()

def save_state_to_db(cursor, state, scope=KV_SCOPE, key=KV_KEY):
    json_str = json.dumps(state, indent=2)
    cursor.execute(
        "INSERT INTO kv (scope, key, value) VALUES (?, ?, ?) ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value;",
        (scope, key, json_str)
    )

# ---------- (Toggle/scan/bulk/reset DIHAPUS 2026-09-05: remap-only. Arsip: git pre-remap-only.) ----------

# ---------- (akhir blok hapus: toggle/scan) ----------


# ---------- (bulk/reset/cycle-reset DIHAPUS 2026-09-05: remap-only) ----------


# ---------- Status snapshot (dipakai Web) ----------

def status_snapshot():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS t, SUM(isActive) AS a FROM providerConnections;")
        r = cur.fetchone()
        total = r["t"] or 0
        configured = r["a"] if r["a"] is not None else total
        cur.execute("SELECT id, provider, data FROM providerConnections;")
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
        conn_data = {}
        for row in conn_rows:
            try:
                data = json.loads(row["data"]) if row["data"] else {}
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            conn_data[row["id"]] = data
        keys = []
        for row in conn_rows:
            cid = row["id"]
            d = conn_data.get(cid, {})
            prov_label = provider_label(row["provider"], d)
            name = (row["name"] or "").strip() or (row["email"] or "").strip() or cid[:8]
            ec = d.get("errorCode")
            last_err = d.get("lastError") or ""
            last_err = " ".join(str(last_err).split())[:80] if last_err else ""
            ket = "-"
            if ec is not None:
                ket = f"{ec} {_hint(ec)}"
                if last_err:
                    ket += f" · {last_err}"
            keys.append({"key": name, "provider": prov_label, "status": "Aktif", "ket": ket})
        keys.sort(key=lambda x: (0 if x["status"] != "Aktif" else 1, x["provider"], x["key"]))
        cur_id = get_cycle_id()
        next_at = (cur_id + 1) * CYCLE_SECONDS
        now_sec = int(time.time())
        remaining = next_at - now_sec
        if remaining < 0:
            remaining = 0
        wib = datetime.datetime.fromtimestamp(next_at, tz=datetime.timezone(datetime.timedelta(hours=7)))
        cycle = {"nextAt": next_at, "remainingSec": remaining, "intervalSec": CYCLE_SECONDS, "wib": wib.strftime("%d %b %H:%M WIB")}
        remap = _remap_snapshot(cur)
        vst = load_state_from_db(cur, VERSION_SCOPE, VERSION_KEY)
        return {
            "total": total,
            "active": configured,
            "by_provider": by_prov,
            "remap": remap,
            "aaVersion": remap.get("ver"),
            "aaVersionChanged": bool(remap.get("ver") and vst.get("prevVer") and remap.get("ver") != vst.get("prevVer")),
            "versionCheck": {"at": vst.get("lastCheckAt"), "error": vst.get("lastCheckError")},
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
    timer = f"Remap berikut: {c.get('wib','-')} (in {hh}j {mm}m)"
    r = s.get("remap") or {}
    ver = r.get("ver") or "-"
    return (
        "⚙️ <b>9RKM — Remap Combo</b>\n\n"
        f"AA ver: {ver}\n"
        f"Key terkonfigurasi: <b>{s['active']}/{s['total']}</b>\n"
        f"Provider: {provs or '-'}\n"
        f"Combo Intel: {r.get('intel', '-')}\n"
        f"{timer}\n\n"
        f"⏱ {s['ts']}"
    )


def tg_handle(update):
    """Update TG dari bot-hub (POST /api/tg). Whitelist chat Dea."""
    cb = update.get("callback_query")
    if cb:
        if str(cb.get("from", {}).get("id")) != TG_CHAT_ID:
            return
        data = (cb.get("data") or "").strip()
        if data == "rkm:status":
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
    else:
        tg_send("Perintah: /status", keyboard=True)

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
        if self.path.startswith("/api/alias/proposal"):
            try:
                r = subprocess.run([sys.executable, os.path.join(UI_PATH, "alias_sync.py")],
                                   capture_output=True, text=True, timeout=180)
                tail = ((r.stdout or "") + (r.stderr or ""))[-2000:]
                try:
                    prop = json.loads(pathlib.Path(os.path.join(UI_PATH, "alias_proposal.json")).read_text(encoding="utf-8"))
                except Exception as e:
                    self._json(500, {"error": f"proposal unreadable: {e}", "log": tail})
                    return
                prop["_sync_rc"] = r.returncode
                prop["_sync_log"] = tail[-1000:]
                self._json(200, prop)
            except Exception as e:
                self._json(500, {"error": str(e)})
            return
        if self.path.startswith("/api/alias/approve"):
            try:
                ln = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(ln).decode()) if ln else {}
            except Exception:
                self._json(400, {"error": "bad json"})
                return
            add = body.get("add") or {}
            remove = body.get("remove") or []
            if not isinstance(add, dict) or not isinstance(remove, list):
                self._json(400, {"error": "need {add:{mid:label}, remove:[mid]}"})
                return
            try:
                import datetime as _dt
                alias_path = os.path.join(UI_PATH, "aa_alias.json")
                cache_path = os.path.join(UI_PATH, "aa_cache.json")
                rows = json.loads(pathlib.Path(cache_path).read_text(encoding="utf-8")).get("data", [])
                names = {(r.get("name") or "").strip() for r in rows}
                for mid, label in add.items():
                    if not isinstance(mid, str) or "/" not in mid or not isinstance(label, str) or not label.strip():
                        self._json(400, {"error": f"bad mapping {mid!r}"})
                        return
                    if label.strip() not in names:
                        self._json(400, {"error": f"label unknown di API: {label}"})
                        return
                alias = json.loads(pathlib.Path(alias_path).read_text(encoding="utf-8")) if os.path.exists(alias_path) else {}
                ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
                bak = alias_path + f".bak-approve-{ts}"
                pathlib.Path(bak).write_text(json.dumps(alias, indent=1, ensure_ascii=False), encoding="utf-8")
                os.chmod(bak, 0o600)
                for mid in remove:
                    alias.pop(mid, None)
                for mid, label in add.items():
                    alias[mid.strip()] = label.strip()
                pathlib.Path(alias_path).write_text(json.dumps(alias, indent=1, ensure_ascii=False), encoding="utf-8")
                os.chmod(alias_path, 0o600)
                log(f"[WebUI] alias approve +{len(add)} -{len(remove)} bak={os.path.basename(bak)}.")
                self._json(200, {"ok": True, "alias_count": len(alias), "added": len(add), "removed": len(remove)})
            except Exception as e:
                self._json(500, {"error": str(e)})
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
    log("=== 9RKM remap-only started (scan/reset/toggle/bulk dihapus 2026-09-05) ===")
    threads = [
        threading.Thread(target=remap_scheduler_thread, daemon=True),
        threading.Thread(target=http_thread, daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

if __name__ == "__main__":
    main()
