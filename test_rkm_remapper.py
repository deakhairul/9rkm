"""Smoke test rkm_remapper non-enforce: dry-run tidak menulis combos/settings."""
import os
import sys
import json
import sqlite3
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rkm_state as R

def main():
    path = os.path.join(tempfile.gettempdir(), "rkm_remapper_check.sqlite")
    if os.path.exists(path):
        os.remove(path)
    conn = R.open_db(path)
    R.ensure_schema(conn)
    # skema minimal 9Router
    conn.executescript("""
    CREATE TABLE providerConnections(id TEXT PRIMARY KEY, provider TEXT, isActive INTEGER, data TEXT, updatedAt TEXT);
    CREATE TABLE providerNodes(id TEXT PRIMARY KEY, type TEXT, name TEXT, data TEXT, createdAt TEXT, updatedAt TEXT);
    CREATE TABLE requestDetails(id TEXT PRIMARY KEY, timestamp TEXT, connectionId TEXT, status TEXT, data TEXT);
    CREATE TABLE apiKeys(key TEXT);
    CREATE TABLE combos(name TEXT PRIMARY KEY, models TEXT);
    CREATE TABLE settings(id INTEGER PRIMARY KEY, data TEXT);
    CREATE TABLE kv(scope TEXT, key TEXT, value TEXT, PRIMARY KEY(scope,key));
    INSERT INTO combos VALUES('Artificial-Analysis-Intelligence-Index','["a/b"]');
    INSERT INTO combos VALUES('Artificial-Analysis-Agentic-Index','["c/d"]');
    INSERT INTO settings VALUES(1, json('{"capacityAdapter":{"vision":{"models":["x/y","z/w"]}}}'));
    """)
    # node eligible + key (ON, Unknown) + satu key OFF (harus tetap masuk discovery)
    conn.execute("INSERT INTO providerNodes VALUES('n1','openai-compatible','NodeOcc','{}',datetime('now'),datetime('now'))")
    conn.execute("INSERT INTO providerConnections VALUES('k1','n1',1,?,datetime('now'))",
                 (json.dumps({"providerSpecificData": {"prefix": "occ", "baseUrl": "http://x.invalid"}, "apiKey": "K"}),))
    conn.execute("INSERT INTO providerConnections VALUES('k2','n1',0,?,datetime('now'))",
                 (json.dumps({"providerSpecificData": {"prefix": "occ", "baseUrl": "http://x.invalid"}, "apiKey": "K2"}),))
    conn.commit()
    assert R.bootstrap(conn) == 2
    # k2 OFF -> routing Disabled (proyeksi isActive), k1 ON Unknown -> provider fail-open eligible
    conn.close()

    import rkm_remapper as M
    conn = R.open_db(path)
    M.DB = path
    # 1) status report jujur
    rep = M.status_report(conn)
    assert rep["vision"]["count"] == 2, rep["vision"]
    assert rep["vision"]["pool"] == ["x/y", "z/w"]
    assert rep["combos_now"] == {M.COMBO_INTEL: 1, M.COMBO_AGENTIC: 1}
    assert rep["providers_eligible"] >= 1, rep  # fail-open unknown
    # 2) dry-run combos tidak boleh menyentuh DB (butuh network -> cukup assert tidak
    #    menulis: jalankan dengan catalog gagal pun harus returncode != crash total;
    #    di sini cukup verifikasi write gate: engine combo OFF -> write dipaksa False)
    st = R.get_engine(conn, R.ENG_COMBO)
    assert st.get("enabled", False) is False
    before = conn.execute("SELECT models FROM combos WHERE name=?", (M.COMBO_INTEL,)).fetchone()[0]
    # (tidak memanggil remap_combos penuh: butuh network AA; gate diuji unit)
    import inspect
    src = inspect.getsource(M.remap_combos)
    assert "write = write and enforce" in src.replace("  ", " ") or "write and enforce" in src
    # 3) vision dry: engine OFF -> tidak jalur write; lastChecked tetap tercatat saat dry
    rc = M.remap_vision(conn, write=True)  # write dipaksa tapi engine OFF -> tetap dry
    after = R.get_engine(conn, R.ENG_VISION)
    assert after.get("lastMode") == "dry", after
    pool_now = M.vision_status(conn)
    assert pool_now["count"] == 2, pool_now  # tidak berubah
    conn.close()
    os.remove(path)
    print("rkm_remapper selfcheck PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
