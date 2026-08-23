#!/usr/bin/env python3
"""
aa_rank.py — sync 2 combo AA (Intelligence + Agentic) dari Data API v2/free
Scope: HANYA Artificial-Analysis-Intelligence-Index & Artificial-Analysis-Agentic-Index
Source: https://artificialanalysis.ai/api/v2/language/models/free (x-api-key)
Aturan: exclude no-score, dedup 1 model/provider (prefix), pure AA (-score tie free), beda len Intel/Agentic tanpa samakan
Jadwal: per 5 jam selaras auto ON 9RKM via cron 0 */5 * * * (00/05/10/15/20 UTC)
  KR: kr/claude-sonnet-4.5 -> Claude Sonnet 4.5 exact (API miss sementara exclude)
  orcarouter: orcarouter/free -> DeepSeek V4 Pro 0813 Max (route DeepSeek)
  occ rate=pass (FreeUsageLimit), ocgc/nvidia pass verif kamu, cerebras/gc removed
"""
import json, sys, os, sqlite3, datetime, urllib.request, urllib.error, pathlib, subprocess, concurrent.futures

DB = os.environ.get("ROUTER_DB", os.path.expanduser("~/.9router/db/data.sqlite"))
ALIAS_PATH = os.path.join(os.path.dirname(__file__), "aa_alias.json")
ROUTER_API = os.environ.get("ROUTER_API", "http://localhost:20128/v1/chat/completions")
PROBE_TIMEOUT = int(os.environ.get("AA_PROBE_TIMEOUT", "15"))
PROBE_WORKERS = int(os.environ.get("AA_PROBE_WORKERS", "8"))
KEY_ENV = "AA_API_KEY"
KEY_FILE_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), ".aa_api_key"),
    os.path.expanduser("~/scripts/9rkm/.aa_api_key"),
    "/home/ubuntu/scripts/9rkm/.aa_api_key",
]
COMBO_INTEL = "Artificial-Analysis-Intelligence-Index"
COMBO_AGENTIC = "Artificial-Analysis-Agentic-Index"
API_BASE = "https://artificialanalysis.ai/api/v2/language/models/free"

def load_api_key():
    if os.environ.get(KEY_ENV):
        return os.environ[KEY_ENV].strip()
    for p in KEY_FILE_CANDIDATES:
        try:
            if os.path.exists(p):
                v = pathlib.Path(p).read_text(encoding="utf-8").strip()
                if v:
                    return v
        except Exception:
            pass
    return ""

def fetch_api_all():
    key = load_api_key()
    if not key:
        raise RuntimeError("AA API key missing: set AA_API_KEY env or write .aa_api_key file")
    rows = []
    page = 1
    intel_ver = None
    while True:
        url = f"{API_BASE}?page={page}"
        req = urllib.request.Request(url, headers={"x-api-key": key, "User-Agent": "9rkm-aa_rank/1.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            body = json.loads(r.read().decode())
            if intel_ver is None:
                intel_ver = body.get("intelligence_index_version")
            tier = r.headers.get("X-AA-Tier")
            rem = r.headers.get("X-RateLimit-Remaining")
            print(f"[aa_rank] API page {page} tier={tier} rem={rem} len={len(body.get('data',[]))} ver={intel_ver}")
            rows.extend(body.get("data", []))
            pag = body.get("pagination", {})
            if not pag.get("has_more"):
                break
            page += 1
            if page > 10:
                break
    return rows, intel_ver

def is_free(mid: str) -> bool:
    return "free" in (mid or "").lower()

def load_alias():
    p = pathlib.Path(ALIAS_PATH)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))

def distinct_models(conn):
    cur = conn.cursor()
    cur.execute("SELECT models FROM combos;")
    s = set()
    for (raw,) in cur.fetchall():
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                s.update(arr)
        except Exception:
            pass
    return sorted(s)

def dedup_by_provider(scored):
    by_prov = {}
    for mid, label, score in scored:
        prov = (mid.split("/")[0] if "/" in mid else mid).lower()
        cur = by_prov.get(prov)
        if cur is None or score > cur[2]:
            by_prov[prov] = (mid, label, score)
    out = list(by_prov.values())
    def key_fn(x):
        mid, _, score = x
        free = 0 if is_free(mid) else 1
        return (-score, free, mid)
    out.sort(key=key_fn)
    return out

def has_active_connection(conn, prefix):
    p = (prefix or "").lower()
    cur = conn.cursor()
    cur.execute("SELECT provider, data FROM providerConnections WHERE isActive=1")
    active_prefixes = set()
    active_providers = set()
    for provider, raw in cur.fetchall():
        active_providers.add((provider or "").lower())
        try:
            d = json.loads(raw) if raw else {}
            psd = d.get("providerSpecificData") if isinstance(d, dict) else None
            if isinstance(psd, dict):
                pre = (psd.get("prefix") or "").strip().lower()
                if pre:
                    active_prefixes.add(pre)
        except Exception:
            pass
    if p in active_prefixes:
        return True
    if p in active_providers:
        return True
    mapping = {
        "ag": ["gemini", "antigravity"],
        "gemini": ["gemini"],
        "gc": ["gemini-cli"],
        "vx": ["vertex"],
        "cc": ["claude"],
        "cx": ["codex"],
        "kc": ["kilocode"],
        "nvidia": ["nvidia"],
        "cerebras": ["cerebras"],
        "groq": ["groq"],
        "mistral": ["mistral"],
        "ollama": ["ollama"],
        "cf": ["cloudflare-ai"],
        "tokenrouter": ["tokenrouter"],
        "bynara": ["openai-compatible-chat-ca41cb40-b185-42e9-a648-5f5937f96f69"],
        "occ": ["openai-compatible-chat-88eeba84-c728-4d9f-aeed-f2c90ff53926"],
        "ocgc": ["openai-compatible-chat-e880bc91-fb90-4526-aeda-4f4816d938cd"],
        "madewgn": ["openai-compatible-chat-702fb81f-b075-4abf-82c4-c30d31087da5"],
        "orcarouter": ["openai-compatible-chat-5da092d7-90ce-48db-b765-301f2bb59ce5", "deepseek"],
        "kr": ["claude", "kr"],
        "zenmux": ["openai-compatible-chat-766044ec-1137-4f87-ad22-42ccc998d898"],
        "oc": ["antigravity"],
    }
    for cand in mapping.get(p, []):
        if cand.lower() in active_providers or cand.lower() in active_prefixes:
            return True
    return False

def router_key_via_db(conn):
    try:
        cur = conn.cursor()
        cur.execute("SELECT key FROM apiKeys LIMIT 1")
        row = cur.fetchone()
        if row and row[0] and str(row[0]).startswith("sk-"):
            return str(row[0])
    except Exception:
        pass
    try:
        import subprocess
        r = subprocess.run(["sqlite3", os.path.expanduser("~/.9router/db/data.sqlite"), "SELECT key FROM apiKeys LIMIT 1;"], capture_output=True, text=True, timeout=5)
        k = (r.stdout or "").strip()
        if k.startswith("sk-"):
            return k
    except Exception:
        pass
    return os.environ.get("ROUTER_KEY","")

def probe_model(mid, api_key, timeout=PROBE_TIMEOUT):
    if not api_key:
        return "fail"
    for max_tok in (4, 16):
        payload = json.dumps({"model": mid, "messages": [{"role": "user", "content": "ping"}], "max_tokens": max_tok, "stream": False})
        try:
            req = urllib.request.Request(ROUTER_API, data=payload.encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
                msg = d.get("choices", [{}])[0].get("message", {})
                return "ok" if msg else "down"
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8","replace")
            except Exception:
                body = str(e)
            if "max_output_tokens" in body or "max_tokens" in body.lower():
                continue
            if "429" in str(e) or "rate_limit" in body or "FreeUsageLimit" in body:
                return "rate"
            if "No active credentials" in body or "model_not_found" in body:
                return "down"
            if "bad_request" in body.lower() and mid.split("/")[0].lower() in body.lower():
                return "ok"
            if "bad_request" in body.lower():
                return "ok"
            if max_tok == 16:
                return "fail"
            continue
        except Exception:
            if max_tok == 16:
                return "fail"
            continue
    return "fail"

def filter_active_and_probe(conn, scored):
    if not scored:
        return scored
    prefixes = { (mid.split("/")[0] if "/" in mid else mid).lower() for mid,_,_ in scored }
    active_ok = {}
    for p in prefixes:
        active_ok[p] = has_active_connection(conn, p)
        if not active_ok[p]:
            print(f"[aa_rank] SKIP prefix {p}: no isActive=1 connection")
    filtered = []
    to_probe = []
    for mid, label, score in scored:
        prov = (mid.split("/")[0] if "/" in mid else mid).lower()
        if not active_ok.get(prov):
            print(f"[aa_rank] EXCLUDE {mid} (prefix {prov} inactive)")
            continue
        to_probe.append((mid, label, score))
    if not to_probe:
        return []
    api_key = router_key_via_db(conn)
    if not api_key:
        print("[aa_rank] WARN no router apiKey, skip probe (keep active only)")
        return to_probe
    def do_probe(item):
        mid,_,_ = item
        st = probe_model(mid, api_key)
        return (item, st)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
        futs = {ex.submit(do_probe, it): it for it in to_probe}
        for f in concurrent.futures.as_completed(futs):
            item, st = f.result()
            results[item[0]] = st
            print(f"[aa_rank] probe {st:5} {item[0]}")
    kept = []
    for mid, label, score in to_probe:
        st = results.get(mid, "fail")
        if st in ("ok","rate"):
            kept.append((mid,label,score))
        else:
            print(f"[aa_rank] EXCLUDE {mid} probe={st}")
    return kept

def main():
    dry = "--dry" in sys.argv
    write = "--write" in sys.argv
    cron = "--cron" in sys.argv
    if cron:
        dry = False
        write = True

    alias = load_alias()
    print(f"[aa_rank] alias {len(alias)} entries")

    rows, ver = fetch_api_all()
    print(f"[aa_rank] API total {len(rows)} ver={ver}")

    intel_map = {}
    agentic_map = {}
    by_name = {}
    for r in rows:
        name = r.get("name","").strip()
        ev = r.get("evaluations",{})
        intel = ev.get("artificial_analysis_intelligence_index")
        agentic = ev.get("artificial_analysis_agentic_index")
        if name:
            by_name[name] = r
            if isinstance(intel, (int,float)):
                intel_map[name] = float(intel)
            if isinstance(agentic, (int,float)):
                agentic_map[name] = float(agentic)
    print(f"[aa_rank] intel_map {len(intel_map)} agentic_map {len(agentic_map)}")

    conn = sqlite3.connect(DB) if os.path.exists(DB) else sqlite3.connect(os.path.expanduser("~/.9router/db/data.sqlite"))
    distinct = distinct_models(conn)
    print(f"[aa_rank] distinct models {len(distinct)}")
    for m in distinct:
        print(f"  - {m} -> {alias.get(m, '?')}")

    def build_scored(score_map, fallback_map=None):
        scored = []
        for mid in distinct:
            label = alias.get(mid)
            score = score_map.get(label) if label and isinstance(score_map.get(label), (int,float)) else None
            if not isinstance(score, (int,float)):
                if fallback_map is not None and label and isinstance(fallback_map.get(label), (int,float)):
                    score = float(fallback_map[label])
                else:
                    continue
            scored.append((mid, label, float(score)))
        return scored

    intel_scored = build_scored(intel_map, agentic_map)
    agentic_scored = build_scored(agentic_map, intel_map)
    print(f"[aa_rank] scored Intel {len(intel_scored)} Agentic {len(agentic_scored)} (before probe)")
    intel_probed = filter_active_and_probe(conn, intel_scored)
    agentic_probed = filter_active_and_probe(conn, agentic_scored)
    print(f"[aa_rank] probed Intel {len(intel_probed)} Agentic {len(agentic_probed)} (pass=[ok,rate])")
    intel_sorted = dedup_by_provider(intel_probed)
    agentic_sorted = dedup_by_provider(agentic_probed)
    print(f"[aa_rank] dedup Intel {len(intel_sorted)} Agentic {len(agentic_sorted)} (keep max score among pass)")
    intel_list = [mid for mid, _, _ in intel_sorted]
    agentic_list = [mid for mid, _, _ in agentic_sorted]
    print(f"[aa_rank] final Intel {len(intel_list)} Agentic {len(agentic_list)} (union fallback MAX, pure AA -score tie free)")

    print("\n[aa_rank] Intelligence sorted (pure AA -score tie free, 1/provider):")
    for mid, label, score in intel_sorted:
        tag = "free" if is_free(mid) else "paid"
        print(f"  {score:6.1f} [{tag:4}] {mid:45} <- {label}")
    print("\n[aa_rank] Agentic sorted (pure AA -score tie free, 1/provider):")
    for mid, label, score in agentic_sorted:
        tag = "free" if is_free(mid) else "paid"
        print(f"  {score:6.1f} [{tag:4}] {mid:45} <- {label}")

    if dry and not write:
        print("\n[aa_rank] --dry done (no DB write)")
        return
    if not write:
        print("\n[aa_rank] nothing to write (use --dry or --write)")
        return

    import shutil, hashlib
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = DB + f".bak-aa-{ts}"
    try:
        shutil.copy2(DB, bak)
        md5 = hashlib.md5(open(bak, "rb").read()).hexdigest()[:8]
        print(f"[aa_rank] backup {bak} md5 {md5}")
    except Exception as e:
        print(f"[aa_rank] backup fail {e}", file=sys.stderr)
        sys.exit(2)

    cur = conn.cursor()
    for name, lst in [(COMBO_INTEL, intel_list), (COMBO_AGENTIC, agentic_list)]:
        cur.execute("UPDATE combos SET models = ? WHERE name = ?", (json.dumps(lst), name))
        if cur.rowcount == 0:
            cur.execute("INSERT INTO combos(name, models) VALUES(?, ?)", (name, json.dumps(lst)))
        print(f"[aa_rank] wrote {name} {len(lst)} models")
    conn.commit()
    cur.execute("SELECT name, json_array_length(models) FROM combos WHERE name IN (?,?)", (COMBO_INTEL, COMBO_AGENTIC))
    for row in cur.fetchall():
        print(f"[aa_rank] verify {row[0]} len={row[1]}")
    try:
        import tg_notify
        msg = f"<b>AA Rank</b> API v2/free sync {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} — Intel {len(intel_list)} Agentic {len(agentic_list)} pureAA 1/provider ver={ver}"
        tg_notify.notify(msg)
    except Exception:
        pass
    conn.close()
    print("[aa_rank] done")

if __name__ == "__main__":
    main()
