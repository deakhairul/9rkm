import aa_rank
import key_manager

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


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
    print(f"PASS {len(tests)}")
