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


def test_no_key_writers_remap_only():
    # Remap-only 2026-09-05: 9RKM tidak boleh menulis providerConnections.isActive,
    # tidak ada scan/reset/bulk/toggle, tidak ada endpoint on/off key.
    import inspect
    assert not hasattr(key_manager, "run_scan_tick")
    assert not hasattr(key_manager, "run_reset")
    assert not hasattr(key_manager, "bulk_activate_all")
    assert not hasattr(key_manager, "bulk_deactivate_all")
    assert not hasattr(key_manager, "get_toggle")
    assert not hasattr(key_manager, "set_toggle")
    assert not hasattr(key_manager, "candidates_from_error_code")
    assert not hasattr(key_manager, "reset_cycle_thread")
    assert hasattr(key_manager, "remap_scheduler_thread")
    src = inspect.getsource(key_manager)
    assert "isActive = 0" not in src, "dilarang mematikan key"
    assert "isActive = 1" not in src, "dilarang menyalakan key"
    assert "/api/toggle" not in src
    assert "/api/keys/" not in src


def test_eligibility_includes_disabled_connections():
    # Saringan = probe, bukan flag: koneksi isActive=0 tetap masuk inventory.
    import sqlite3 as _sqlite3
    import tempfile as _tempfile
    import os as _os
    tmp = _tempfile.mkdtemp()
    db = _os.path.join(tmp, "e.sqlite")
    conn = _sqlite3.connect(db)
    conn.execute("CREATE TABLE providerConnections(provider TEXT, data TEXT, isActive INT)")
    conn.execute("INSERT INTO providerConnections VALUES('groq', '{\"providerSpecificData\": {\"prefix\": \"groq\"}}', 0)")
    conn.commit()
    try:
        inv = aa_rank.connection_inventory(conn)
        assert "groq" in inv, "koneksi OFF harus tetap eligible discovery"
        assert aa_rank.has_active_connection(conn, "groq") is True
    finally:
        conn.close()
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


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


# (test_run_reset_activates_all DIHAPUS 2026-09-05: reset tidak ada lagi — remap-only)


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


# ---------- Remap-only behavioral tests (SQLite temp, tanpa framework) ----------

def _mk_conn(db, cid, active=1):
    conn = _sqlite3.connect(db)
    conn.execute("INSERT OR REPLACE INTO providerConnections VALUES(?,?,?,?,?,?,?)", (cid, "p-" + cid, "k-" + cid, "a@b", active, "{}", "2026-09-05T10:00:00.000Z"))
    conn.commit()
    conn.close()


def test_version_change_triggers_once_then_cooldown():
    import time as _time
    km, db, tmp = _lifecycle_env()
    try:
        km._fetch_aa_version = lambda timeout=25: "4.2"
        km._remap_ver = lambda: "4.1"
        changed, live, base = km._check_version_changed()
        assert (changed, live, base) == (True, "4.2", "4.1")
        # scheduler mencatat trigger sukses → cek berikut cooldown
        km._save_cycle_state({**km._cycle_state(), "lastVersionTrig": _time.time()})
        changed2, _, _ = km._check_version_changed()
        assert changed2 is False, "cooldown harus menunda trigger kedua"
        # setelah remap menulis ver baru → tidak ada perubahan
        km._remap_ver = lambda: "4.2"
        km._save_cycle_state({**km._cycle_state(), "lastVersionTrig": 0})
        changed3, live3, base3 = km._check_version_changed()
        assert changed3 is False and live3 == "4.2" and base3 == "4.2"
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def test_version_check_failure_is_recorded_not_fatal():
    km, db, tmp = _lifecycle_env()
    try:
        def boom(timeout=25):
            raise RuntimeError("jaringan putus")
        km._fetch_aa_version = boom
        changed, live, base = km._check_version_changed()
        assert changed is False and live is None
        vst = km._read_version_state()
        assert vst.get("lastCheckError"), "gagal cek harus tercatat"
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def test_schedule_due_logic():
    km, db, tmp = _lifecycle_env()
    try:
        km.get_cycle_id = lambda: 777
        assert km._due_schedule() is True, "belum ada successCycle = jatuh tempo"
        km._save_cycle_state({"successCycle": 777, "at": "x", "status": "ok"})
        assert km._due_schedule() is False
        km.get_cycle_id = lambda: 778
        assert km._due_schedule() is True
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def test_status_snapshot_remap_only_shape():
    km, db, tmp = _lifecycle_env()
    try:
        _mk_conn(db, "c1", active=1)
        _mk_conn(db, "c2", active=0)
        snap = km.status_snapshot()
        assert "enabled" not in snap and "toggle" not in snap
        assert "problem_count" not in snap and "bulk_last" not in snap
        assert snap["total"] == 2
        assert snap["aaVersion"] is None
        assert snap["versionCheck"] == {"at": None, "error": None}
        keys = {k["key"]: k for k in snap["keys"]}
        assert set(keys) == {"k-c1", "k-c2"}
        assert all(k["status"] == "Aktif" for k in keys.values()), "tidak ada label OFF"
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def test_slug_top20_requires_explicit_alias():
    rows = [row("Top Model", "top-model", 99, 90), row("Low Model", "low-model", 10, 9)]
    catalog = [
        {"id": "vendor/top-model", "owned_by": "vendor"},
        {"id": "vendor/low-model", "owned_by": "vendor"},
    ]
    groups, unmatched = aa_rank.build_ranked_candidates(
        catalog, {}, rows, INTEL, AGENTIC, {"vendor"}, {"Top Model"})
    assert unmatched == 1
    assert [item["mid"] for item in groups["vendor"]] == ["vendor/low-model"]


def test_slug_outside_top20_still_allowed():
    rows = [row("Top Model", "top-model", 99, 90), row("Low Model", "low-model", 10, 9)]
    catalog = [
        {"id": "vendor/top-model", "owned_by": "vendor"},
        {"id": "vendor/low-model", "owned_by": "vendor"},
    ]
    groups, unmatched = aa_rank.build_ranked_candidates(
        catalog, {}, rows, INTEL, AGENTIC, {"vendor"}, {"Top Model", "Other"})
    assert unmatched == 1
    aliases = {"vendor/top-model": "Top Model"}
    groups2, unmatched2 = aa_rank.build_ranked_candidates(
        catalog, aliases, rows, INTEL, AGENTIC, {"vendor"}, {"Top Model"})
    assert unmatched2 == 0
    assert [item["mid"] for item in groups2["vendor"]] == ["vendor/top-model", "vendor/low-model"]


def test_quorum_blocks_minority_active_prefix():
    import sqlite3 as _sqlite3
    import tempfile as _tempfile
    import os as _os
    tmp = _tempfile.mkdtemp()
    db = _os.path.join(tmp, "q.sqlite")
    conn = _sqlite3.connect(db)
    conn.execute("CREATE TABLE providerConnections(provider TEXT, data TEXT, isActive INT)")
    for i in range(4):
        conn.execute("INSERT INTO providerConnections VALUES('openrouter', '{}', ?)", (1 if i == 0 else 0,))
    conn.execute("INSERT INTO providerConnections VALUES('groq', '{}', 1)")
    conn.commit()
    catalog = [{"id": "openrouter/x", "owned_by": "openrouter"}, {"id": "groq/y", "owned_by": "groq"}]
    try:
        assert aa_rank.active_route_prefixes(conn, catalog) == {"groq"}
        assert aa_rank.active_route_prefixes(conn, catalog, quorum=0.25) == {"groq", "openrouter"}
    finally:
        conn.close()
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


def test_stream_ok_sse_success():
    raw = 'data: {"choices": [{"delta": {"content": "PONG"}}]}\n\ndata: [DONE]\n'
    assert key_manager._stream_ok(raw) is True


def test_stream_ok_sse_error():
    raw = 'data: {"error": {"message": "bad"}}\n\ndata: [DONE]\n'
    assert key_manager._stream_ok(raw) is False


def test_stream_ok_plain_choices():
    assert key_manager._stream_ok('{"choices": [{"message": {"content": "PONG"}}]}') is True
    assert key_manager._stream_ok('{"error": "nope", "choices": []}') is False


# (test_scan_cap DIHAPUS 2026-09-05: tidak ada scan — remap-only)


def test_rollback_marks_visible_state_truthful():
    km, db, tmp = _lifecycle_env()
    try:
        conn = _sqlite3.connect(db)
        conn.execute("INSERT INTO combos VALUES('Artificial-Analysis-Intelligence-Index', ?)",
                     (_json.dumps(["a/x", "b/y"]),))
        conn.execute("INSERT INTO kv VALUES('aa_remap', 'state', ?)",
                     (_json.dumps({"at": "lama", "source": "api", "intel": 99, "coverage": {}, "vision": 5, "ver": "4.1"}),))
        conn.commit()
        conn.close()
        km._mark_remap_rolled_back("uji-gagal")
        conn = _sqlite3.connect(db)
        row = conn.execute("SELECT value FROM kv WHERE scope='aa_remap' AND key='state'").fetchone()
        conn.close()
        st = _json.loads(row[0])
        assert st["source"].startswith("rollback:")
        assert st["intel"] == 2
        assert st["vision"] == 5
        assert st["ver"] == "4.1", "rollback harus pertahankan versi"
        assert st["rollback"] is True
    finally:
        _os.environ.pop("ROUTER_DB", None)
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"PASS {len(tests)}")
