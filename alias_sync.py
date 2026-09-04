#!/usr/bin/env python3
"""
alias_sync.py — usulan perawatan alias AA (default read-only).

- Baca label + skor Intelligence dari cache API (aa_cache.json), atau --refresh
  untuk tarik API segar dulu (pakai kredensial aa_rank).
- Bandingkan dengan aa_alias.json + katalog 9Router:
  * unmapped   : label skor-top tanpa alias (dengan saran mid + confidence)
  * stale      : alias menunjuk mid tak ada di katalog / label hilang dari API
  * superseded : alias versi lama padahal label versi baru ada (pin versi)
- Tulis usulan ke alias_proposal.json. Penerapan HANYA via Web UI approve
  (atau persetujuan eksplisit Dea) — script ini tidak mengubah alias.
"""
import json
import os
import pathlib
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ALIAS_PATH = os.path.join(HERE, "aa_alias.json")
CACHE_PATH = os.path.join(HERE, "aa_cache.json")
PROPOSAL_PATH = os.path.join(HERE, "alias_proposal.json")
TOPK = 50


def log(msg):
    print(msg, flush=True)


def load_cache():
    p = pathlib.Path(CACHE_PATH)
    if not p.exists():
        raise RuntimeError(f"cache {CACHE_PATH} tidak ada; jalankan aa_rank --fetch dulu")
    raw = json.loads(p.read_text(encoding="utf-8"))
    rows = raw.get("data") if isinstance(raw, dict) and "data" in raw else raw
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("cache kosong")
    return rows


def load_aa_rank():
    import importlib.util
    spec = importlib.util.spec_from_file_location("aa_rank_sync", os.path.join(HERE, "aa_rank.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def toks(text):
    return re.findall(r"[a-z]+|\d+", (text or "").lower())


def ver_nums(text):
    return re.findall(r"\d+(?:\.\d+)?", text or "")


def split_base_ver(label):
    m = re.match(r"^(.*?)[\s\-_v]*(\d+(?:\.\d+)*)\s*(\([^)]*\))?\s*$", (label or "").strip())
    if not m:
        return (label or "").strip().lower(), ()
    base = m.group(1).strip().lower()
    ver = tuple(int(x) for x in m.group(2).split("."))
    return base, ver


def fuzzy_mid_label(mid, label):
    suffix = mid.split("/", 1)[1] if "/" in mid else mid
    cand_tokens = set(toks(suffix)) | set(toks(suffix.rsplit("/", 1)[-1]))
    lab_tokens = set(toks(label))
    if not cand_tokens or not lab_tokens:
        return 0.0
    inter = cand_tokens & lab_tokens
    union = cand_tokens | lab_tokens
    score = len(inter) / len(union)
    mv, lv = set(ver_nums(suffix)), set(ver_nums(label))
    if mv and lv:
        score += 0.25 if (mv & lv) else -0.15
    return round(max(0.0, min(1.0, score)), 2)


def main():
    refresh = "--refresh" in sys.argv
    A = load_aa_rank()
    if refresh:
        rows, ver, tier, rem = A.fetch_api_all()
        A.save_cache(rows, ver, tier, rem)
        log(f"[alias_sync] cache refreshed n={len(rows)} ver={ver}")
    rows = load_cache()
    alias = {}
    if os.path.exists(ALIAS_PATH):
        alias = json.loads(pathlib.Path(ALIAS_PATH).read_text(encoding="utf-8"))
    by_name = {}
    intel = {}
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        by_name[name] = r
        score = (r.get("evaluations") or {}).get("artificial_analysis_intelligence_index")
        if isinstance(score, (int, float)):
            intel[name] = float(score)
    try:
        conn, _ = A._open_conn()
        catalog = A.fetch_router_catalog(conn)
        conn.close()
        mids = [str(m.get("id") or "") for m in catalog if "/" in str(m.get("id") or "")]
    except Exception as e:
        log(f"[alias_sync] katalog gagal dibaca ({e}) — saran mid dilewati")
        mids = []
    mapped_labels = set(alias.values())
    top = sorted(intel.items(), key=lambda kv: -kv[1])[:TOPK]
    unmapped = []
    for label, score in top:
        if label in mapped_labels:
            continue
        best, best_conf = None, 0.0
        for mid in mids:
            conf = fuzzy_mid_label(mid, label)
            if conf > best_conf:
                best, best_conf = mid, conf
        unmapped.append({"label": label, "score": score,
                         "suggest_mid": best if best_conf >= 0.35 else None,
                         "confidence": best_conf if best_conf >= 0.35 else None})
    midset = set(mids)
    stale = []
    for mid, label in sorted(alias.items()):
        if mids and mid not in midset:
            stale.append({"mid": mid, "label": label, "reason": "mid-not-in-catalog"})
        elif label not in by_name:
            stale.append({"mid": mid, "label": label, "reason": "label-gone-from-api"})
    by_base = {}
    for name in by_name:
        base, ver = split_base_ver(name)
        by_base.setdefault(base, []).append((ver, name))
    newest = {b: max(v for v, _ in vs) for b, vs in by_base.items() if any(v for v, _ in vs)}
    superseded = []
    for mid, label in sorted(alias.items()):
        base, ver = split_base_ver(label)
        if ver and base in newest and ver < newest[base]:
            cand = [n for v, n in by_base[base] if v == newest[base]]
            if cand and cand[0] != label:
                superseded.append({"mid": mid, "from": label, "to": cand[0]})
    proposal = {"at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
                "alias_count": len(alias), "topk": TOPK,
                "unmapped": unmapped, "stale": stale, "superseded": superseded}
    tmp = PROPOSAL_PATH + ".tmp"
    pathlib.Path(tmp).write_text(json.dumps(proposal, indent=1, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, PROPOSAL_PATH)
    log(f"[alias_sync] alias={len(alias)} unmapped_top{TOPK}={len(unmapped)} "
        f"saran_mid={sum(1 for u in unmapped if u['suggest_mid'])} stale={len(stale)} superseded={len(superseded)}")
    log(f"[alias_sync] proposal -> {PROPOSAL_PATH} (read-only, tanpa ubah alias)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
