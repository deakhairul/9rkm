#!/usr/bin/env python3
"""
rkm_remapper.py - 9RKM v2 Combo & Vision Remapper (2026-09-01)

Perbedaan dari aa_rank.py legacy (ADR 0005):
  1. Discovery = SEMUA provider terkonfigurasi (rkm_key_state), bukan
     hanya yang punya key isActive=1 saat itu -> provider all-OFF tetap
     masuk discovery (menutup lubang provider terkunci keluar).
  2. Eligibility combo = provider dengan >=1 key
     Desired=Enabled + Routing=Enabled + Health=Healthy (atau fallback
     Health=Unknown saat belum ada yang Healthy — fail-open terkendali
     supaya combo tidak kosong sebelum enforce v2; dicatat di log),
     tanpa Provider Incident aktif.
  3. Vision = lifecycle terpisah; status JUJUR: jumlah aktual tersimpan
     di settings + lastChecked; "skipped" tidak pernah ditulis 0.
  4. Non-enforce (engine combo/vision enabled=false): mode --dry wajib;
     tidak menulis combos/settings apa pun.

CLI:
  --combos [--write]   remap 1 combo AA (default dry-run)
  --vision [--write]   remap vision pool (default dry-run)
  --status             laporan eligibility + status vision aktual
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rkm_state as R

DB = os.environ.get("ROUTER_DB", "/home/ubuntu/.9router/db/data.sqlite")
COMBO_INTEL = "Artificial-Analysis-Intelligence-Index"


def log(msg):
    R.log(f"[remap-v2] {msg}")


# ---------- provider identity: route prefix -> node id ----------

def route_prefix_map(conn):
    """Map route-prefix katalog (tokenrouter, occ, kr, ...) -> set node id provider.
    Prefix hidup di providerConnections.data.providerSpecificData (per-key),
    BUKAN di providerNodes.data."""
    m = {}
    rows = conn.execute("SELECT provider, data FROM providerConnections").fetchall()
    for r in rows:
        try:
            d = json.loads(r["data"]) if r["data"] else {}
        except Exception:
            continue
        psd = d.get("providerSpecificData") or {}
        pfx = (psd.get("prefix") or "").strip().lower()
        if pfx:
            m.setdefault(pfx, set()).add((r["provider"] or "").lower())
    return m


def provider_health_classes(conn):
    """Klasifikasi semua provider terkonfigurasi menurut state v2.
    Return: {node_id: {"eligible": bool, "reason": str, "prefix": str}}"""
    rows = conn.execute("""
      SELECT provider_node_id,
        SUM(CASE WHEN desired='Enabled' AND routing='Enabled' AND health='Healthy' THEN 1 ELSE 0 END) healthy,
        SUM(CASE WHEN desired='Enabled' AND routing='Enabled' AND health='Unknown' THEN 1 ELSE 0 END) unknown_ok,
        COUNT(*) total
      FROM rkm_key_state GROUP BY provider_node_id
    """).fetchall()
    incident = {r["incident_domain"] for r in conn.execute(
        "SELECT incident_domain FROM rkm_provider_incident WHERE status != 'Closed'").fetchall()}
    out = {}
    for r in rows:
        p = r["provider_node_id"]
        if p in incident:
            out[p] = {"eligible": False, "reason": "incident", "healthy": r["healthy"], "unknown": r["unknown_ok"]}
        elif r["healthy"] > 0:
            out[p] = {"eligible": True, "reason": "healthy-key", "healthy": r["healthy"], "unknown": r["unknown_ok"]}
        elif r["unknown_ok"] > 0:
            # fail-open terkendali: v2 belum enforce -> Unknown masih boleh menyetor
            # kandidat; saat enforce penuh, ganti ke eligible=False reason=awaiting-proof
            out[p] = {"eligible": True, "reason": "unknown-key(fail-open)", "healthy": 0, "unknown": r["unknown_ok"]}
        else:
            out[p] = {"eligible": False, "reason": "no-enabled-key", "healthy": 0, "unknown": 0}
    return out


# ---------- status ----------

def vision_status(conn):
    row = conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
    if not row or not row["data"]:
        return {"pool": None, "lastChecked": None}
    try:
        d = json.loads(row["data"])
    except Exception:
        return {"pool": None, "lastChecked": None}
    pool = ((d.get("capacityAdapter") or {}).get("vision") or {}).get("models")
    st = R.get_engine(conn, R.ENG_VISION)
    return {"pool": pool, "lastChecked": st.get("lastChecked"), "count": len(pool) if isinstance(pool, list) else None}


def status_report(conn):
    classes = provider_health_classes(conn)
    st_combo = R.get_engine(conn, R.ENG_COMBO)
    st_vision = R.get_engine(conn, R.ENG_VISION)
    combos = {}
    for name in (COMBO_INTEL,):
        row = conn.execute("SELECT models FROM combos WHERE name=?", (name,)).fetchone()
        import json as J
        combos[name] = len(J.loads(row["models"])) if row and row["models"] else 0
    eligible = {p: c for p, c in classes.items() if c["eligible"]}
    return {
        "ts": R.now_iso(),
        "combo_engine_enabled": st_combo.get("enabled", False),
        "vision_engine_enabled": st_vision.get("enabled", False),
        "providers_configured": len(classes),
        "providers_eligible": len(eligible),
        "eligible": {p: c["reason"] for p, c in sorted(eligible.items())},
        "combos_now": combos,
        "vision": vision_status(conn),
    }


# ---------- remap combos ----------

def remap_combos(conn, write=False):
    """Discovery penuh memakai aa_rank dengan inventory V2 (semua provider
    terkonfigurasi, eligibility state-machine). Gagal = nol efek."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("aa_rank", os.path.join(HERE, "aa_rank.py"))
    A = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(A)

    st = R.get_engine(conn, R.ENG_COMBO)
    enforce = st.get("enabled", False)
    write = write and enforce  # ponytail: non-enforce hanya dry — buka saat cutover combo
    if not write:
        log("mode DRY (engine combo belum enforce atau tanpa --write)")

    classes = provider_health_classes(conn)
    eligible_nodes = {p for p, c in classes.items() if c["eligible"]}

    # inventory v2: SEMUA provider terkonfigurasi (bukan hanya isActive) — pakai
    # data node + key; untuk probe tetap lewat gateway (key aktif yang melayani).
    def all_configured_inventory(conn2):
        inv = A.connection_inventory(conn2)  # prefix -> [data aktif] (untuk lock probe)
        rows = conn2.execute("SELECT provider, data FROM providerConnections").fetchall()
        for r in rows:
            try:
                d = json.loads(r["data"]) if r["data"] else {}
            except Exception:
                continue
            psd = d.get("providerSpecificData") or {}
            pfx = (psd.get("prefix") or "").strip().lower()
            pid = (r["provider"] or "").lower()
            # prefix map sederhana: node id dicover via route_prefix_map
        return inv

    # jalankan discovery aa_rank dengan inventory diperluas:
    # trik minimal: monkeypatch has_active_connection & connection_inventory
    # agar menganggap key provider ELIGIBLE (meski OFF) sebagai aktif-untuk-discovery.
    prefix_nodes = route_prefix_map(conn)
    eligible_prefixes = set()
    for pfx, nodes in prefix_nodes.items():
        if nodes & eligible_nodes:
            eligible_prefixes.add(pfx)

    orig_has_active = A.has_active_connection
    orig_inventory = A.connection_inventory

    def v2_has_active(conn2, prefix):
        if (prefix or "").lower() in eligible_prefixes:
            return True
        return orig_has_active(conn2, prefix)

    def v2_inventory(conn2, inventory=None):
        inv = dict(orig_inventory(conn2))
        # tambahkan key OFF milik provider eligible supaya lock-filter &
        # katalog per-provider tetap menyala
        rows = conn2.execute("SELECT provider, data FROM providerConnections").fetchall()
        for r in rows:
            try:
                d = json.loads(r["data"]) if r["data"] else {}
            except Exception:
                continue
            psd = d.get("providerSpecificData") or {}
            pfx = (psd.get("prefix") or "").strip().lower()
            if pfx and pfx in eligible_prefixes and pfx not in inv:
                inv.setdefault(pfx, []).append(d)
        return inv

    A.has_active_connection = v2_has_active
    A.connection_inventory = v2_inventory
    try:
        rc = A._do_remap_unlocked(write=write, with_vision=False, dry=not write, use_cache=True)
    finally:
        A.has_active_connection = orig_has_active
        A.connection_inventory = orig_inventory
    if rc == 0:
        R.record_event(conn, "combo_remap_v2", None, None,
                       json.dumps({"write": write, "eligible_prefixes": sorted(eligible_prefixes)}))
        st_combo = R.get_engine(conn, R.ENG_COMBO)
        R.set_engine(conn, R.ENG_COMBO, {**st_combo, "lastRun": R.now_iso(),
                                         "lastMode": "write" if write else "dry",
                                         "eligible": len(eligible_prefixes)}, by="remapper-v2")
    log(f"combos rc={rc} write={write} eligible_prefixes={sorted(eligible_prefixes)}")
    return rc


# ---------- remap vision ----------

def remap_vision(conn, write=False):
    import importlib.util
    spec = importlib.util.spec_from_file_location("aa_rank", os.path.join(HERE, "aa_rank.py"))
    A = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(A)

    st = R.get_engine(conn, R.ENG_VISION)
    enforce = st.get("enabled", False)
    write = write and enforce
    before = vision_status(conn)
    if not write:
        log(f"vision DRY — pool saat ini {before.get('count')} (tidak diubah; skipped ≠ 0)")

    rc = 0
    if write:
        A_child_rc = A._do_remap_unlocked(write=False, with_vision=True, dry=True, use_cache=True)
        # ponytail: vision write path aa_rank digabung di _do_remap_unlocked;
        # jalur vision-only legacy ada di main(). Untuk v2 gunakan:
        rc = _run_vision_write(A, conn)
    if rc == 0:
        after = vision_status(conn)
        R.set_engine(conn, R.ENG_VISION, {**st, "lastChecked": R.now_iso(),
                                          "lastCount": after.get("count"),
                                          "lastMode": "write" if write else "dry"}, by="remapper-v2")
        R.record_event(conn, "vision_remap_v2", None, None,
                       json.dumps({"mode": "write" if write else "dry",
                                   "count_before": before.get("count"),
                                   "count_after": after.get("count")}))
        log(f"vision rc=0 mode={'write' if write else 'dry'} pool={after.get('count')} lastChecked diupdate")
    return rc


def _run_vision_write(A, conn):
    """Vision-only write via jalur legacy --vision (backup+rollback milik aa_rank)."""
    import subprocess, tempfile
    # aa_rank main() --vision --write menangani backup sendiri; panggil sebagai child
    r = subprocess.run([sys.executable, os.path.join(HERE, "aa_rank.py"), "--vision", "--write"],
                       capture_output=True, text=True, timeout=1200)
    (r.stdout or "") and log(r.stdout[-800:])
    (r.stderr or "") and log("STDERR " + r.stderr[-500:])
    return r.returncode


def main():
    conn = R.open_db(DB)
    R.ensure_schema(conn)
    args = sys.argv[1:]
    write = "--write" in args
    if "--status" in args:
        print(json.dumps(status_report(conn), indent=1))
        return
    if "--combos" in args:
        sys.exit(remap_combos(conn, write=write))
    if "--vision" in args:
        sys.exit(remap_vision(conn, write=write))
    print(__doc__)
    conn.close()


if __name__ == "__main__":
    main()
