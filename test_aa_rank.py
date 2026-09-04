import aa_rank
import key_manager
import datetime as _datetime

INTEL = "artificial_analysis_intelligence_index"
AGENTIC = "artificial_analysis_agentic_index"


def row(name, slug, intel=None, agentic=None):
    return {
        "id": slug,
        "name": name,
        "slug": slug,
        "evaluations": {INTEL: intel, AGENTIC: agentic},
    }


def test_openrouter_glm_beats_hy3():
    rows = [row("GLM-5.2 (max)", "glm-5-2", 52.6, 45.7), row("Hy3", "hy3", 42.2, 31.4)]
    catalog = [
        {"id": "openrouter/tencent/hy3:free", "owned_by": "openrouter"},
        {"id": "openrouter/z-ai/glm-5.2:free", "owned_by": "openrouter"},
    ]
    groups, unmatched = aa_rank.build_ranked_candidates(catalog, {}, rows, INTEL, AGENTIC, {"openrouter"})
    assert unmatched == 0
    assert [item["mid"] for item in groups["openrouter"]] == [
        "openrouter/z-ai/glm-5.2:free",
        "openrouter/tencent/hy3:free",
    ]
    selected = aa_rank.select_working_candidates(groups, lambda _: "ok", workers=1)
    assert [item["mid"] for item in selected] == ["openrouter/z-ai/glm-5.2:free"]


def test_paid_failure_falls_to_free():
    rows = [row("GLM-5.2 (max)", "glm-5-2", 52.6, 45.7)]
    catalog = [
        {"id": "vendor/z-ai/glm-5.2", "owned_by": "vendor"},
        {"id": "vendor/z-ai/glm-5.2:free", "owned_by": "vendor"},
    ]
    groups, _ = aa_rank.build_ranked_candidates(catalog, {}, rows, INTEL, AGENTIC, {"vendor"})
    statuses = {"vendor/z-ai/glm-5.2": "down", "vendor/z-ai/glm-5.2:free": "ok"}
    selected = aa_rank.select_working_candidates(groups, statuses.get, workers=1)
    assert [item["mid"] for item in selected] == ["vendor/z-ai/glm-5.2:free"]


def test_payment_skips_remaining_paid_then_uses_free():
    rows = [
        row("Paid A", "paid-a", 60, 50),
        row("Paid B", "paid-b", 59, 49),
        row("Free C", "free-c", 55, 45),
    ]
    catalog = [
        {"id": "vendor/paid-a", "owned_by": "vendor"},
        {"id": "vendor/paid-b", "owned_by": "vendor"},
        {"id": "vendor/free-c:free", "owned_by": "vendor"},
    ]
    groups, _ = aa_rank.build_ranked_candidates(catalog, {}, rows, INTEL, AGENTIC, {"vendor"})
    calls = []
    def probe(mid):
        calls.append(mid)
        return "payment" if mid.endswith("paid-a") else "ok"
    selected = aa_rank.select_working_candidates(groups, probe, workers=1)
    assert [item["mid"] for item in selected] == ["vendor/free-c:free"]
    assert calls == ["vendor/paid-a", "vendor/free-c:free"]


def test_429_falls_to_next_model():
    rows = [row("Model A", "model-a", 60, 50), row("Model B", "model-b", 55, 45)]
    catalog = [
        {"id": "vendor/model-a", "owned_by": "vendor"},
        {"id": "vendor/model-b", "owned_by": "vendor"},
    ]
    groups, _ = aa_rank.build_ranked_candidates(catalog, {}, rows, INTEL, AGENTIC, {"vendor"})
    selected = aa_rank.select_working_candidates(groups, lambda mid: "rate" if mid.endswith("model-a") else "ok", workers=1)
    assert [item["mid"] for item in selected] == ["vendor/model-b"]


def test_target_index_precedes_fallback_index():
    rows = [row("Agentic", "agentic", 10, 20), row("Intel Only", "intel-only", 90, None)]
    catalog = [
        {"id": "vendor/agentic", "owned_by": "vendor"},
        {"id": "vendor/intel-only", "owned_by": "vendor"},
    ]
    groups, _ = aa_rank.build_ranked_candidates(catalog, {}, rows, AGENTIC, INTEL, {"vendor"})
    assert [item["mid"] for item in groups["vendor"]] == ["vendor/agentic", "vendor/intel-only"]


def test_probe_cache_reused_between_indexes():
    rows = [row("Model A", "model-a", 60, 50)]
    catalog = [{"id": "vendor/model-a", "owned_by": "vendor"}]
    intel, _ = aa_rank.build_ranked_candidates(catalog, {}, rows, INTEL, AGENTIC, {"vendor"})
    agentic, _ = aa_rank.build_ranked_candidates(catalog, {}, rows, AGENTIC, INTEL, {"vendor"})
    calls = []
    cache = {}
    probe = lambda mid: calls.append(mid) or "ok"
    aa_rank.select_working_candidates(intel, probe, cache, workers=1)
    aa_rank.select_working_candidates(agentic, probe, cache, workers=1)
    assert calls == ["vendor/model-a"]


def test_alias_override_and_no_fuzzy_match():
    rows = [row("Exact Model", "exact-model", 40, 30)]
    by_name, by_slug = aa_rank.aa_indexes(rows)
    assert aa_rank.resolve_aa_row("vendor/odd-name", {"vendor/odd-name": "Exact Model"}, by_name, by_slug)["name"] == "Exact Model"
    assert aa_rank.resolve_aa_row("vendor/exact-model-v2", {}, by_name, by_slug) is None


def test_model_lock_prefilter_requires_all_keys_locked():
    catalog = [{"id": "vendor/model-a", "owned_by": "vendor"}]
    locked = {"modelLock_model-a": "2099-01-01T00:00:00Z"}
    unlocked = {"modelLock_model-a": "2000-01-01T00:00:00Z"}
    kept, skipped = aa_rank.filter_catalog_by_locks(catalog, {"vendor": [locked, locked]})
    assert kept == [] and skipped == 1
    kept, skipped = aa_rank.filter_catalog_by_locks(catalog, {"vendor": [locked, unlocked]})
    assert kept == catalog and skipped == 0


def test_scan_pauses_during_remap():
    key_manager.REMAP_PROBING.set()
    try:
        assert key_manager.run_scan_tick() == (0, "remap-probing")
    finally:
        key_manager.REMAP_PROBING.clear()


def test_free_classifier():
    assert aa_rank.is_free("openrouter/z-ai/glm-5.2:free")
    assert aa_rank.is_free("vendor/free/model")
    assert aa_rank.is_free("vendor/model-free")
    assert not aa_rank.is_free("vendor/freedom-model")
    assert aa_rank.is_free("vendor/x/free")


def test_model_is_free_pricing_first():
    priced = {"id": "vendor/freedom-model", "pricing": {"prompt": 0, "completion": 0}}
    assert aa_rank.model_is_free(priced) is True
    paid = {"id": "vendor/model:free", "pricing": {"prompt": 1.5, "completion": 2}}
    assert aa_rank.model_is_free(paid) is False
    no_pricing = {"id": "vendor/model:free"}
    assert aa_rank.model_is_free(no_pricing) is True


def test_no_retire_concept():
    assert not hasattr(key_manager, "MAX_FAILED_CYCLES")
    assert not hasattr(key_manager, "reconcile_state_and_connections")
    import inspect
    src = inspect.getsource(key_manager)
    assert "is_retired" not in src, "retire harus hilang total dari key_manager"


def test_vision_probe_400_is_down():
    import ast, inspect
    tree = ast.parse(inspect.getsource(aa_rank.probe_vision_native))
    branches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
        and any(isinstance(c, ast.Constant) and c.value == "bad_request" for c in ast.walk(node.test))
    ]
    assert branches, "harus ada cabang bad_request di probe vision"
    for node in branches:
        returns = [n.value.value for n in node.body if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)]
        assert returns == ["down"], f"bad_request harus down, dapat {returns}"


def test_filter_vision_native_does_not_touch_settings():
    import inspect
    src = inspect.getsource(aa_rank.filter_vision_native)
    assert "UPDATE settings" not in src, "probe vision tidak boleh memutasi settings gateway"


def test_do_remap_lock_is_flock_not_oexcl():
    import inspect
    src = inspect.getsource(aa_rank.do_remap)
    assert "O_EXCL" not in src, "lock standalone harus flock"
    assert "fcntl.flock" in src
    assert "os.remove(lock_path)" not in src, "tidak boleh hapus lock proses lain"


def test_run_reset_activates_all_regardless_of_state():
    import inspect
    src = inspect.getsource(key_manager.run_reset)
    assert "WHERE id IN" in src and "isActive = 1" in src, "reset mengaktifkan SEMUA key tanpa filter"


# ---------- Lifecycle behavioral tests (SQLite temp, tanpa framework) ----------

import json as _json
import os as _os
import sqlite3 as _sqlite3
import tempfile as _tempfile
import importlib.util as _importlib_util


def _lifecycle_env():
    """Setup DB temp + modul key_manager terisolasi (tanpa side-effect global)."""
    tmp = _tempfile.mkdtemp(prefix="9rkm-lc-")
    db = _os.path.join(tmp, "lc.sqlite")
    _os.environ["ROUTER_DB"] = db
    _os.environ["RKM_UI_PATH"] = tmp
    _os.environ["RKM_HTTP_PORT"] = "18999"
    _os.environ["RKM_HTTP_HOST"] = "127.0.0.1"
    conn = _sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE kv(scope TEXT, key TEXT, value TEXT, PRIMARY KEY(scope,key));
    CREATE TABLE providerConnections(id TEXT PRIMARY KEY, provider TEXT, name TEXT, email TEXT, isActive INTEGER, data TEXT, updatedAt TEXT);
    CREATE TABLE requestDetails(id INTEGER PRIMARY KEY, timestamp TEXT, connectionId TEXT, status TEXT, data TEXT);
    CREATE TABLE apiKeys(key TEXT);
    CREATE TABLE combos(name TEXT PRIMARY KEY, models TEXT);
    """)
    conn.commit()
    conn.close()
    spec = _importlib_util.spec_from_file_location("km_lc", _os.path.join(_os.path.dirname(__file__), "key_manager.py"))
    km = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(km)
    return km, db, tmp


def _fresh_ts():
    # error fresh relatif waktu sekarang (mencegah test basi saat tanggal lewat window 1 jam)
    # Presisi ms wajib: format detik-bulat ("000Z") membuat err_ts < success_ts artifisial
    # sehingga candidates_from_error_code mengira sukses-lebih-baru dan tes jadi flaky
    # (lolos hanya bila sleep 0.25s melintasi batas detik).
    return _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _mk_conn(db, cid, active=1, error=None, err_at=None):
    err_at = err_at or _fresh_ts()
    conn = _sqlite3.connect(db)
    existing = conn.execute("SELECT data FROM providerConnections WHERE id=?", (cid,)).fetchone()
    data = _json.loads(existing[0]) if existing else {}
    if error is not None:
        data.update({"errorCode": error, "lastError": "x", "lastErrorAt": err_at})
    else:
        data.pop("errorCode", None); data.pop("lastError", None); data.pop("lastErrorAt", None)
    conn.execute("INSERT OR REPLACE INTO providerConnections VALUES(?,?,?,?,?,?,?)", (cid, "p-" + cid, "k-" + cid, "a@b", active, _json.dumps(data), "2026-08-31T10:00:00.000Z"))
    conn.commit()
    conn.close()


def _mk_req(db, cid, ts, status):
    conn = _sqlite3.connect(db)
    conn.execute("INSERT INTO requestDetails(timestamp, connectionId, status, data) VALUES(?,?,?,?)", (ts, cid, status, "{}"))
    conn.commit()
    conn.close()


def _state(db, cid):
    conn = _sqlite3.connect(db)
    row = conn.execute("SELECT value FROM kv WHERE scope='hourly_key_disable' AND key='state'").fetchone()
    conn.close()
    return (_json.loads(row[0]) if row else {}).get(cid, {})


def _cycle(n):
    return 332000 + n  # id siklus dummy berurutan


def test_lifecycle_three_cycles_reach_S3():
    km, db, tmp = _lifecycle_env()
    km.REMAP_PROBING.clear()
    try:
        for i in range(3):
            _mk_conn(db, "c1", active=1, error=401, err_at=_fresh_ts())
            km.get_cycle_id = lambda: _cycle(i)
            km.run_scan_tick()
            assert _state(db, "c1")["failed_cycles"] == i + 1, f"siklus {i}: {_state(db, 'c1')}"
            assert km._state_row_active(db, "c1") == 0
            # reset sukses remap: aktifkan lagi, error dibersihkan, counter PERTAHAN
            _mk_conn(db, "c1", active=1)
            km.run_reset()
            st = _state(db, "c1")
            assert st["failed_cycles"] == i + 1, f"reset hapus counter di siklus {i}"
            assert km._state_row_active(db, "c1") == 1
        snap = km.status_snapshot()
        assert snap["problem_count"] == 1
        assert snap["problem"][0]["failed_cycles"] == 3
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def test_lifecycle_same_cycle_no_double_count():
    km, db, tmp = _lifecycle_env()
    try:
        km.get_cycle_id = lambda: _cycle(0)
        for round in range(2):
            _mk_conn(db, "c1", active=1, error=402, err_at=_fresh_ts())
            km.run_scan_tick()
            _mk_conn(db, "c1", active=1)  # reaktivasi tanpa sukses
        assert _state(db, "c1")["failed_cycles"] == 1
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def test_lifecycle_real_success_clears_counter():
    km, db, tmp = _lifecycle_env()
    try:
        km.get_cycle_id = lambda: _cycle(0)
        _mk_conn(db, "c1", active=1, error=429, err_at=_fresh_ts())
        km.run_scan_tick()
        assert _state(db, "c1")["failed_cycles"] == 1
        auto_off_ts = _state(db, "c1")["auto_off_ts"]
        # sukses nyata SETELAH auto_off_ts (dalam window 1 jam scan) → counter reset
        _mk_conn(db, "c1", active=1)
        import datetime as _dt
        base = _dt.datetime.fromisoformat(auto_off_ts.replace("Z", "+00:00"))
        succ_ts = (base + _dt.timedelta(milliseconds=100)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        _mk_req(db, "c1", succ_ts, "success")
        import time as _time
        _time.sleep(0.25)  # pastikan succ_ts <= now saat tick
        km.run_scan_tick()
        st = _state(db, "c1")
        assert st.get("failed_cycles", 0) == 0, st
        assert "auto_off_ts" not in st
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def test_lifecycle_success_then_new_fail_restarts_at_1():
    km, db, tmp = _lifecycle_env()
    try:
        km.get_cycle_id = lambda: _cycle(1)
        _mk_conn(db, "c1", active=1, error=403, err_at=_fresh_ts())
        km.run_scan_tick()
        # streak 1, sukses lalu fail lagi dalam siklus BARU → restart dari 1
        _mk_conn(db, "c1", active=1)
        auto_off_ts = _state(db, "c1")["auto_off_ts"]
        import datetime as _dt
        base = _dt.datetime.fromisoformat(auto_off_ts.replace("Z", "+00:00"))
        succ_ts = (base + _dt.timedelta(milliseconds=100)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        _mk_req(db, "c1", succ_ts, "success")
        import time as _time
        _time.sleep(0.25)
        km.get_cycle_id = lambda: _cycle(2)
        _mk_conn(db, "c1", active=1, error=403, err_at=_fresh_ts())
        km.run_scan_tick()
        assert _state(db, "c1")["failed_cycles"] == 1, "sukses lalu gagal = streak baru mulai 1"
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def test_lifecycle_activate_all_preserves_counter():
    km, db, tmp = _lifecycle_env()
    try:
        km.get_cycle_id = lambda: _cycle(0)
        _mk_conn(db, "c1", active=1, error=401, err_at=_fresh_ts())
        km.run_scan_tick()
        km.bulk_activate_all("test")
        st = _state(db, "c1")
        assert st.get("failed_cycles") == 1, "ACTIVATE ALL tidak boleh hapus counter"
        assert km._state_row_active(db, "c1") == 1
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def test_lifecycle_deactivate_all_not_a_failure():
    km, db, tmp = _lifecycle_env()
    try:
        _mk_conn(db, "c1", active=1)
        km.bulk_deactivate_all("test")
        assert _state(db, "c1").get("failed_cycles", 0) == 0
        snap = km.status_snapshot()
        k = [x for x in snap["keys"] if x["key"] == "k-c1"]
        assert k and k[0]["status"] == "OFF (bulk)", k
        # reset → aktif, lalu auto-off otomatis → label OFF (bukan stale OFF bulk)
        km.run_reset()
        km.get_cycle_id = lambda: _cycle(0)
        # error HARUS lebih baru dari bulk_at+10s (grace) agar jadi kandidat
        c = _sqlite3.connect(db)
        bulk_at = _json.loads(c.execute("SELECT value FROM kv WHERE scope='rkm_bulk' AND key='last'").fetchone()[0])["at"]
        c.close()
        base = _datetime.datetime.fromisoformat(bulk_at.replace("Z", "+00:00"))
        err_ts = (base + _datetime.timedelta(seconds=11)).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
        _mk_conn(db, "c1", active=1, error=401, err_at=err_ts)
        km.run_scan_tick()
        snap = km.status_snapshot()
        k = [x for x in snap["keys"] if x["key"] == "k-c1"]
        assert k and k[0]["status"] == "OFF", k
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def test_lifecycle_phantom_kv_ignored():
    km, db, tmp = _lifecycle_env()
    try:
        conn = _sqlite3.connect(db)
        ghost = {"ghost-1": {"failed_cycles": 9, "provider": "x", "name": "ghost"}}
        conn.execute("INSERT INTO kv VALUES('hourly_key_disable','state',?)", (_json.dumps(ghost),))
        conn.commit()
        conn.close()
        snap = km.status_snapshot()
        assert snap["problem_count"] == 0, "KV tanpa connection nyata tidak boleh jadi problem"
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def test_lifecycle_active_problem_shows_S():
    km, db, tmp = _lifecycle_env()
    try:
        _mk_conn(db, "c1", active=1)
        conn = _sqlite3.connect(db)
        st = {"c1": {"failed_cycles": 3, "provider": "p-c1", "name": "k-c1"}}
        conn.execute("INSERT INTO kv VALUES('hourly_key_disable','state',?)", (_json.dumps(st),))
        conn.commit()
        conn.close()
        snap = km.status_snapshot()
        k = [x for x in snap["keys"] if x["key"] == "k-c1"][0]
        assert "S3" in k["ket"], k
        assert k["status"] == "Aktif"
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"PASS {len(tests)}")
