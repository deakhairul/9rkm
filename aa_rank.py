#!/usr/bin/env python3
"""
aa_rank.py — sync 2 combo AA (Intelligence + Agentic) + Vision dari Data API v2/free
Scope: Artificial-Analysis-Intelligence-Index & Artificial-Analysis-Agentic-Index + Vision-Adapter
Source: https://artificialanalysis.ai/api/v2/language/models/free (x-api-key)
Aturan: exclude no-score, dedup 1 model/provider (prefix), pure AA (-score tie free), beda len Intel/Agentic tanpa samakan
Jadwal: fetch 1x/hari 08:00 WIB tulis aa_cache.json (1 hit API), remap per 5 jam baca cache (0 hit API), manual POST /api/remap trigger async
  - Jika fetch gagal 429/limit/tier habis -> fallback baca aa_cache.json / kv aa_cache (DB fallback)
  KR: kr/claude-sonnet-4.5 -> Claude Sonnet 4.5 exact, orcarouter/free -> DeepSeek V4 Pro 0813 Max, occ rate=pass, ocgc/nvidia pass
"""
import json, sys, os, sqlite3, time, datetime, urllib.request, urllib.error, pathlib, subprocess, concurrent.futures, hashlib, time

DB = os.environ.get("ROUTER_DB", os.path.expanduser("~/.9router/db/data.sqlite"))
ALIAS_PATH = os.path.join(os.path.dirname(__file__), "aa_alias.json")
ROUTER_API = os.environ.get("ROUTER_API", "http://localhost:20128/v1/chat/completions")
PROBE_TIMEOUT = int(os.environ.get("AA_PROBE_TIMEOUT", "25"))
PROBE_WORKERS = int(os.environ.get("AA_PROBE_WORKERS", "8"))
KEY_ENV = "AA_API_KEY"
KEY_FILE_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), ".aa_api_key"),
    os.path.expanduser("~/scripts/9rkm/.aa_api_key"),
    "/home/ubuntu/scripts/9rkm/.aa_api_key",
]
CACHE_DIR_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), ""),
    os.path.expanduser("~/scripts/9rkm"),
    "/home/ubuntu/scripts/9rkm",
]
COMBO_INTEL = "Artificial-Analysis-Intelligence-Index"
COMBO_AGENTIC = "Artificial-Analysis-Agentic-Index"
COMBO_VISION = "Vision-Adapter-AA-Vision"
API_BASE = "https://artificialanalysis.ai/api/v2/language/models/free"

def _cache_path():
    for d in CACHE_DIR_CANDIDATES:
        try:
            if d and os.path.isdir(d):
                return os.path.join(d, "aa_cache.json")
        except Exception:
            pass
    return os.path.join(os.path.dirname(__file__), "aa_cache.json")

CACHE_PATH = os.environ.get("AA_CACHE_PATH", _cache_path())
KV_CACHE_SCOPE = "aa_cache"
KV_CACHE_KEY = "state"
KV_REMAP_SCOPE = "aa_remap"
KV_REMAP_KEY = "state"

def log(msg, **kw):
    print(msg, flush=True, **{k: v for k, v in kw.items() if k in ("file",)})

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

def save_cache(rows, ver, tier=None, rem=None):
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {"fetchedAt": ts, "ver": ver, "tier": tier, "rem": rem, "n": len(rows), "data": rows, "md5": hashlib.md5(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:8]}
    p = pathlib.Path(CACHE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(p) + ".tmp"
    pathlib.Path(tmp).write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, str(p))
    log(f"[aa_rank] cache saved {p} n={len(rows)} ver={ver} tier={tier} rem={rem} md5={payload['md5']}")
    try:
        conn = sqlite3.connect(DB) if os.path.exists(DB) else None
        if conn:
            cur = conn.cursor()
            kv_val = json.dumps({"at": ts, "n": len(rows), "ver": ver, "md5": payload["md5"], "path": str(p)})
            cur.execute("INSERT INTO kv(scope,key,value) VALUES(?,?,?) ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value", (KV_CACHE_SCOPE, KV_CACHE_KEY, kv_val))
            conn.commit()
            conn.close()
    except Exception as e:
        log(f"[aa_rank] kv cache save fail {e}")
    return payload

def load_cache():
    for src in [CACHE_PATH, os.path.expanduser("~/scripts/9rkm/aa_cache.json")]:
        try:
            if os.path.exists(src):
                raw = json.loads(pathlib.Path(src).read_text(encoding="utf-8"))
                rows = raw.get("data") if isinstance(raw, dict) and "data" in raw else (raw if isinstance(raw, list) else [])
                ver = raw.get("ver") if isinstance(raw, dict) else None
                if isinstance(rows, list) and rows:
                    log(f"[aa_rank] cache hit file {src} n={len(rows)} ver={ver} at={raw.get('fetchedAt','')}")
                    return rows, ver
        except Exception as e:
            log(f"[aa_rank] cache file fail {src}: {e}")
    try:
        conn = sqlite3.connect(DB) if os.path.exists(DB) else None
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM kv WHERE scope=? AND key=?", (KV_CACHE_SCOPE, KV_CACHE_KEY))
            row = cur.fetchone()
            conn.close()
            if row and row[0]:
                j = json.loads(row[0])
                md5 = j.get("md5")
                log(f"[aa_rank] cache hit kv md5={md5} at={j.get('at','')}")
    except Exception:
        pass
    return None, None

def fetch_api_all():
    key = load_api_key()
    if not key:
        raise RuntimeError("AA API key missing: set AA_API_KEY env or write .aa_api_key file")
    rows = []
    page = 1
    intel_ver = None
    tier_last = None
    rem_last = None
    while True:
        url = f"{API_BASE}?page={page}"
        req = urllib.request.Request(url, headers={"x-api-key": key, "User-Agent": "9rkm-aa_rank/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                body = json.loads(r.read().decode())
                if intel_ver is None:
                    intel_ver = body.get("intelligence_index_version")
                tier_last = r.headers.get("X-AA-Tier") or tier_last
                rem_last = r.headers.get("X-RateLimit-Remaining") or r.headers.get("x-ratelimit-remaining") or rem_last
                log(f"[aa_rank] API page {page} tier={tier_last} rem={rem_last} len={len(body.get('data',[]))} ver={intel_ver}")
                rows.extend(body.get("data", []))
                pag = body.get("pagination", {})
                if not pag.get("has_more"):
                    break
                page += 1
                if page > 10:
                    break
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = str(e)
            low = body.lower()
            is_rate = e.code == 429 or "rate" in low or "limit" in low or "quota" in low
            log(f"[aa_rank] API HTTPError {e.code} page={page} rate={is_rate} body={body[:600]}")
            if is_rate:
                raise RuntimeError(f"AA 429 rate-limit page {page}: {body[:400]}")
            raise
    return rows, intel_ver, tier_last, rem_last

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
    s = set(load_alias())
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
        "openrouter": ["openrouter"],
        "bynara": ["openai-compatible-chat-ca41cb40-b185-42e9-a648-5f5937f96f69"],
        "occ": ["openai-compatible-chat-88eeba84-c728-4d9f-aeed-f2c90ff53926"],
        "ocgc": ["openai-compatible-chat-e880bc91-fb90-4526-aeda-4f4816d938cd"],
        "madewgn": ["openai-compatible-chat-702fb81f-b075-4abf-82c4-c30d31087da5"],
        "orcarouter": ["openai-compatible-chat-5da092d7-90ce-48db-b765-301f2bb59ce5", "deepseek"],
        "kr": ["kiro"],
        "zenmux": ["openai-compatible-chat-766044ec-1137-4f87-ad22-42ccc998d898"],
        "ocgcc": ["openai-compatible-chat-e880bc91-fb90-4526-aeda-4f4816d938cd"],
        "ocgcr": ["openai-compatible-responses-b66965b1-8f0b-4915-bad6-6f2afd60de51"],
        "qwen": ["openai-compatible-chat-75bfd34f-a67c-4311-96b1-a7d1f238ccbe"],
        "oc": ["antigravity"],
        "ocr": ["openai-compatible-responses-59d9ff3d-ceb8-4958-a5f9-da2a7e0a3a2e"],
    }
    for cand in mapping.get(p, []):
        if cand.lower() in active_providers or cand.lower() in active_prefixes:
            return True
    return False

def probe_vision_native(mid, api_key, timeout=PROBE_TIMEOUT):
    if not api_key:
        return "fail"
    PNG_1X1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
    for max_tok in (16, 32):
        payload = json.dumps({"model": mid, "messages": [{"role": "user", "content": [{"type": "text", "text": "describe this 1x1 image one word"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG_1X1}}]}], "max_tokens": max_tok, "stream": False})
        try:
            req = urllib.request.Request(ROUTER_API, data=payload.encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
                msg = d.get("choices", [{}])[0].get("message", {})
                content = msg.get("content")
                if content is not None and str(content).strip():
                    return "ok"
                if msg:
                    return "ok"
                return "down"
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = str(e)
            low = body.lower()
            if "does not support" in low and "image" in low:
                return "no_vision"
            if "unsupported" in low and "image" in low:
                return "no_vision"
            if "429" in str(e) or "rate_limit" in low or "freeusagelimit" in low:
                return "rate"
            if "no active credentials" in low or "model_not_found" in low:
                return "down"
            if "bad_request" in low:
                if _is_model_rejected(body) or _is_payment_rejected(body):
                    return "down" if _is_model_rejected(body) else "down"
                return "ok"
            if max_tok == 32:
                return "fail"
            time.sleep(PROBE_RETRY_SLEEP)
            continue
        except Exception:
            if max_tok == 32:
                return "fail"
            time.sleep(PROBE_RETRY_SLEEP)
    return "fail"

def filter_vision_native(conn, scored):
    if not scored:
        return scored
    api_key = router_key_via_db(conn)
    if not api_key:
        log("[aa_rank] WARN no router apiKey, skip vision probe")
        return scored
    tmp_rows = conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
    orig = None
    if tmp_rows:
        try:
            orig = json.loads(tmp_rows[0])
        except Exception:
            orig = None
    if orig and orig.get("capacityAdapter", {}).get("vision", {}).get("enabled"):
        try:
            tmp = json.loads(tmp_rows[0])
            tmp["capacityAdapter"]["vision"]["enabled"] = False
            conn.execute("UPDATE settings SET data=? WHERE id=1", (json.dumps(tmp),))
            conn.commit()
            log("[aa_rank] vision adapter disabled for native probe")
        except Exception as ex:
            log(f"[aa_rank] adapter disable fail {ex}")
    def do_probe(item):
        mid, _, _ = item
        st = probe_vision_native(mid, api_key)
        return (item, st)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, 4)) as ex:
        futs = {ex.submit(do_probe, it): it for it in scored}
        for f in concurrent.futures.as_completed(futs):
            item, st = f.result()
            results[item[0]] = st
            log(f"[aa_rank] vision probe {st:10} {item[0]}")
    if orig and orig.get("capacityAdapter", {}).get("vision", {}).get("enabled"):
        try:
            cur = conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
            cur_data = json.loads(cur[0]) if cur else {}
            cur_data["capacityAdapter"] = orig.get("capacityAdapter", cur_data.get("capacityAdapter", {}))
            conn.execute("UPDATE settings SET data=? WHERE id=1", (json.dumps(cur_data),))
            conn.commit()
            log("[aa_rank] vision adapter restored")
        except Exception as ex:
            log(f"[aa_rank] adapter restore fail {ex}")
    kept = []
    for mid, label, score in scored:
        st = results.get(mid, "fail")
        if st == "ok":
            kept.append((mid, label, score))
        else:
            log(f"[aa_rank] vision EXCLUDE {mid} probe={st}")
    return kept

def build_vision_pool(conn, intel_map, agentic_map, alias):
    scored = []
    for mid in distinct_models(conn):
        label = alias.get(mid)
        if not label:
            continue
        score = intel_map.get(label)
        if not isinstance(score, (int, float)):
            score = agentic_map.get(label)
        if not isinstance(score, (int, float)):
            continue
        scored.append((mid, label, float(score)))
    if not scored:
        return []
    scored.sort(key=lambda x: (-x[2], 0 if is_free(x[0]) else 1, x[0]))
    probed = filter_vision_native(conn, scored)
    free_ok = [x for x in probed if is_free(x[0])]
    paid_ok = [x for x in probed if not is_free(x[0])]
    free_dedup = dedup_by_provider(free_ok)
    paid_dedup = dedup_by_provider(paid_ok)
    seen = {m.split("/")[0].lower() for m, _, _ in free_dedup}
    paid_filtered = [x for x in paid_dedup if x[0].split("/")[0].lower() not in seen]
    pool = free_dedup + paid_filtered
    log(f"[aa_rank] vision pool free {len(free_dedup)} paid {len(paid_filtered)} total {len(pool)} (free-first, native probe, 1/provider)")
    return pool

def write_vision_adapter(conn, pool):
    cur_data_row = conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
    if not cur_data_row:
        log("[aa_rank] no settings row", file=sys.stderr)
        return False
    data = json.loads(cur_data_row[0])
    if "capacityAdapter" not in data:
        data["capacityAdapter"] = {}
    pool_ids = [mid for mid, _, _ in pool]
    data["capacityAdapter"]["vision"] = {"enabled": True, "roundRobin": True, "models": pool_ids}
    conn.execute("UPDATE settings SET data=? WHERE id=1", (json.dumps(data),))
    conn.commit()
    log(f"[aa_rank] wrote capacityAdapter.vision {len(pool_ids)} models")
    for mid, label, score in pool:
        tag = "free" if is_free(mid) else "paid"
        log(f"  {score:6.1f} [{tag:4}] {mid:45} <- {label}")
    return True

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
        r = subprocess.run(["sqlite3", os.path.expanduser("~/.9router/db/data.sqlite"), "SELECT key FROM apiKeys LIMIT 1;"], capture_output=True, text=True, timeout=5)
        k = (r.stdout or "").strip()
        if k.startswith("sk-"):
            return k
    except Exception:
        pass
    return os.environ.get("ROUTER_KEY","")

PROBE_MAX_TOKENS = (16, 64)
PROBE_RETRY_SLEEP = int(os.environ.get("AA_PROBE_RETRY_SLEEP", "30"))

PAYMENT_REJECT_MARKERS = (
    "insufficient credit",
    "insufficient balance",
    "top up",
    "payment_required",
)

def _is_payment_rejected(body):
    low = (body or "").lower()
    return any(m in low for m in PAYMENT_REJECT_MARKERS)

MODEL_REJECT_MARKERS = (
    "model is not available",
    "model does not exist",
    "model not found",
    "model_not_found",
    "unknown model",
    "invalid model",
    "unsupported model",
    "model is unavailable",
)

def _is_model_rejected(body):
    low = (body or "").lower()
    return any(m in low for m in MODEL_REJECT_MARKERS)

def probe_model(mid, api_key, timeout=PROBE_TIMEOUT):
    if not api_key:
        return "fail"
    for max_tok in PROBE_MAX_TOKENS:
        payload = json.dumps({"model": mid, "messages": [{"role": "user", "content": "ping"}], "max_tokens": max_tok, "stream": False})
        try:
            req = urllib.request.Request(ROUTER_API, data=payload.encode(), headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
                msg = d.get("choices", [{}])[0].get("message", {})
                return "ok" if isinstance(msg, dict) else "down"
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8","replace")
            except Exception:
                body = str(e)
            if "max_output_tokens" in body or "max_tokens" in body.lower():
                time.sleep(PROBE_RETRY_SLEEP)
                continue
            if _is_payment_rejected(body):
                return "down"
            if "429" in str(e) or "rate_limit" in body or "FreeUsageLimit" in body:
                return "rate"
            if "No active credentials" in body or "model_not_found" in body:
                return "down"
            if _is_model_rejected(body):
                return "down"
            if max_tok == PROBE_MAX_TOKENS[-1]:
                return "fail"
            time.sleep(PROBE_RETRY_SLEEP)
            continue
        except Exception:
            if max_tok == PROBE_MAX_TOKENS[-1]:
                return "fail"
            time.sleep(PROBE_RETRY_SLEEP)
            continue
    return "fail"

_CATALOG_CACHE = {}

def _provider_catalog(conn, prefix):
    """Ambil set model-id dari /v1/models node openai-compatible. None = tak bisa dicek."""
    if prefix in _CATALOG_CACHE:
        return _CATALOG_CACHE[prefix]
    ids = None
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, data FROM providerNodes")
        node_id = base = None
        for nid, raw in cur.fetchall():
            d = json.loads(raw) if raw else {}
            if (d.get("prefix") or "").lower() == prefix:
                node_id, base = nid, (d.get("baseUrl") or "").rstrip("/")
                break
        if node_id and base:
            cur.execute(
                "SELECT data FROM providerConnections WHERE provider=? AND isActive=1 LIMIT 1",
                (node_id,))
            row = cur.fetchone()
            key = (json.loads(row[0]).get("apiKey") or "") if row else ""
            if key:
                req = urllib.request.Request(
                    base + "/models",
                    headers={"Authorization": f"Bearer {key}", "User-Agent": "9rkm-aa_rank/1.0"})
                with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
                    body = json.loads(r.read().decode())
                ids = {m.get("id") for m in body.get("data", []) if m.get("id")} or None
    except Exception as e:
        log(f"[aa_rank] katalog {prefix} tak terbaca ({e}); lewati validasi")
        ids = None
    _CATALOG_CACHE[prefix] = ids
    return ids

def in_provider_catalog(conn, mid):
    """False hanya bila katalog terbaca DAN nama model tidak ada di dalamnya."""
    if "/" not in mid:
        return True
    prefix, model = mid.split("/", 1)
    ids = _provider_catalog(conn, prefix.lower())
    return True if ids is None else model in ids

def filter_active_and_probe(conn, scored):
    if not scored:
        return scored
    prefixes = { (mid.split("/")[0] if "/" in mid else mid).lower() for mid,_,_ in scored }
    active_ok = {}
    for p in prefixes:
        active_ok[p] = has_active_connection(conn, p)
        if not active_ok[p]:
            log(f"[aa_rank] SKIP prefix {p}: no isActive=1 connection")
    filtered = []
    to_probe = []
    for mid, label, score in scored:
        prov = (mid.split("/")[0] if "/" in mid else mid).lower()
        if not active_ok.get(prov):
            log(f"[aa_rank] EXCLUDE {mid} (prefix {prov} inactive)")
            continue
        if not in_provider_catalog(conn, mid):
            log(f"[aa_rank] EXCLUDE {mid} (tidak ada di katalog /v1/models {prov})")
            continue
        to_probe.append((mid, label, score))
    if not to_probe:
        return []
    api_key = router_key_via_db(conn)
    if not api_key:
        log("[aa_rank] WARN no router apiKey, skip probe (keep active only)")
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
            log(f"[aa_rank] probe {st:5} {item[0]}")
    kept = []
    for mid, label, score in to_probe:
        st = results.get(mid, "fail")
        if st in ("ok","rate"):
            kept.append((mid,label,score))
        else:
            log(f"[aa_rank] EXCLUDE {mid} probe={st}")
    return kept

def _open_conn():
    p = DB if os.path.exists(DB) else os.path.expanduser("~/.9router/db/data.sqlite")
    return sqlite3.connect(p), p

def do_fetch():
    log(f"[aa_rank] FETCH 1x/hari cache={CACHE_PATH}")
    try:
        rows, ver, tier, rem = fetch_api_all()
        save_cache(rows, ver, tier, rem)
        log(f"[aa_rank] fetch ok n={len(rows)} ver={ver}")
        return 0
    except RuntimeError as e:
        msg = str(e)
        low = msg.lower()
        if "429" in msg or "rate" in low or "limit" in low:
            log(f"[aa_rank] fetch 429 -> fallback cache DB (instruksi Dea: gagal API pakai DB) keep cache lama")
            rows, ver = load_cache()
            if rows:
                log(f"[aa_rank] fallback cache n={len(rows)} ver={ver} -> keep (no write)")
                return 0
            log("[aa_rank] no cache fallback available -> fail", file=sys.stderr)
            return 3
        log(f"[aa_rank] fetch fail {e}", file=sys.stderr)
        return 2
    except Exception as e:
        log(f"[aa_rank] fetch fail {e}", file=sys.stderr)
        return 2

def do_remap(write=True, with_vision=True, dry=False, use_cache=True):
    # LOCK: cegah concurrent dengan Web POST / cron
    lock_path = "/tmp/9rkm-remap.lock"
    try:
        if os.environ.get("AA_REMAP_LOCK_HELD") != "1" and os.path.exists(lock_path) and (time.time() - os.path.getmtime(lock_path) < 300):
            log(f"[aa_rank] SKIP remap locked age={(time.time() - os.path.getmtime(lock_path)):.0f}s")
            return 5
    except Exception:
        pass
    alias = load_alias()
    log(f"[aa_rank] REMAP alias {len(alias)} cache={use_cache} write={write} with_vision={with_vision} dry={dry}")
    rows = None
    ver = None
    source = "api"
    if use_cache:
        rows, ver = load_cache()
        if rows:
            source = "cache"
            log(f"[aa_rank] remap source=cache n={len(rows)} ver={ver}")
    if rows is None:
        try:
            rows, ver, _, _ = fetch_api_all()
            source = "api"
            save_cache(rows, ver)
            log(f"[aa_rank] remap source=api n={len(rows)} ver={ver} (cache refreshed)")
        except RuntimeError as e:
            low = str(e).lower()
            if "429" in str(e) or "rate" in low or "limit" in low:
                log(f"[aa_rank] remap API 429 -> fallback DB cache")
                rows, ver = load_cache()
                if rows:
                    source = "cache-fallback"
                    log(f"[aa_rank] fallback cache n={len(rows)} ver={ver}")
                else:
                    log("[aa_rank] no cache fallback -> abort", file=sys.stderr)
                    return 3
            else:
                raise
    by_name = {}
    intel_map = {}
    agentic_map = {}
    for r in rows or []:
        name = (r.get("name") or "").strip()
        ev = r.get("evaluations") or {}
        intel = ev.get("artificial_analysis_intelligence_index")
        agentic = ev.get("artificial_analysis_agentic_index")
        if name:
            by_name[name] = r
            if isinstance(intel, (int, float)):
                intel_map[name] = float(intel)
            if isinstance(agentic, (int, float)):
                agentic_map[name] = float(agentic)
    log(f"[aa_rank] intel_map {len(intel_map)} agentic_map {len(agentic_map)} source={source}")
    conn, db_path = _open_conn()
    distinct = distinct_models(conn)
    log(f"[aa_rank] distinct models {len(distinct)}")
    for m in distinct:
        log(f"  - {m} -> {alias.get(m, '?')}")
    def build_scored(score_map, fallback_map=None):
        scored = []
        for mid in distinct:
            label = alias.get(mid)
            score = score_map.get(label) if label and isinstance(score_map.get(label), (int, float)) else None
            if not isinstance(score, (int, float)):
                if fallback_map is not None and label and isinstance(fallback_map.get(label), (int, float)):
                    score = float(fallback_map[label])
                else:
                    continue
            scored.append((mid, label, float(score)))
        return scored
    intel_scored = build_scored(intel_map, agentic_map)
    agentic_scored = build_scored(agentic_map, intel_map)
    log(f"[aa_rank] scored Intel {len(intel_scored)} Agentic {len(agentic_scored)} (before probe)")
    intel_probed = filter_active_and_probe(conn, intel_scored)
    agentic_probed = filter_active_and_probe(conn, agentic_scored)
    log(f"[aa_rank] probed Intel {len(intel_probed)} Agentic {len(agentic_probed)} (pass=[ok,rate])")
    intel_sorted = dedup_by_provider(intel_probed)
    agentic_sorted = dedup_by_provider(agentic_probed)
    log(f"[aa_rank] dedup Intel {len(intel_sorted)} Agentic {len(agentic_sorted)} (keep max score among pass)")
    intel_list = [mid for mid, _, _ in intel_sorted]
    agentic_list = [mid for mid, _, _ in agentic_sorted]
    log(f"[aa_rank] final Intel {len(intel_list)} Agentic {len(agentic_list)} (union fallback MAX, pure AA -score tie free)")
    log("\n[aa_rank] Intelligence sorted (pure AA -score tie free, 1/provider):")
    for mid, label, score in intel_sorted:
        tag = "free" if is_free(mid) else "paid"
        log(f"  {score:6.1f} [{tag:4}] {mid:45} <- {label}")
    log("\n[aa_rank] Agentic sorted (pure AA -score tie free, 1/provider):")
    for mid, label, score in agentic_sorted:
        tag = "free" if is_free(mid) else "paid"
        log(f"  {score:6.1f} [{tag:4}] {mid:45} <- {label}")
    vision_pool = None
    if with_vision:
        try:
            vision_pool = build_vision_pool(conn, intel_map, agentic_map, alias)
        except Exception as e:
            log(f"[aa_rank] vision pool fail {e}")
    if dry and not write:
        log("\n[aa_rank] --dry done (no DB write)")
        if vision_pool:
            log(f"[aa_rank] vision pool preview {len(vision_pool)}")
            for mid, label, score in vision_pool:
                tag = "free" if is_free(mid) else "paid"
                log(f"  {score:6.1f} [{tag:4}] {mid:45} <- {label}")
        conn.close()
        return 0
    if not write:
        log("\n[aa_rank] nothing to write (use --dry or --write)")
        conn.close()
        return 0
    import shutil, hashlib as _hl
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = db_path + f".bak-aa-{ts}"
    try:
        shutil.copy2(db_path, bak)
        md5 = _hl.md5(open(bak, "rb").read()).hexdigest()[:8]
        log(f"[aa_rank] backup {bak} md5 {md5}")
    except Exception as e:
        log(f"[aa_rank] backup fail {e}", file=sys.stderr)
        conn.close()
        sys.exit(2)
    cur = conn.cursor()
    # GUARD: jangan tulis combo kosong — pakai DB lama (fallback database)
    prev_intel = None
    prev_agentic = None
    prev_vision = None
    try:
        cur.execute("SELECT models FROM combos WHERE name=?", (COMBO_INTEL,))
        r = cur.fetchone()
        if r and r[0]:
            prev_intel = json.loads(r[0])
    except Exception:
        pass
    try:
        cur.execute("SELECT models FROM combos WHERE name=?", (COMBO_AGENTIC,))
        r = cur.fetchone()
        if r and r[0]:
            prev_agentic = json.loads(r[0])
    except Exception:
        pass
    try:
        row = conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
        if row and row[0]:
            d = json.loads(row[0])
            prev_vision = d.get("capacityAdapter", {}).get("vision", {}).get("models")
    except Exception:
        pass
    if not intel_list:
        log(f"[aa_rank] GUARD skip write {COMBO_INTEL} empty -> keep DB {len(prev_intel) if prev_intel else 0}")
        if prev_intel is not None:
            intel_list = prev_intel
            intel_sorted = [(m, alias.get(m, m), 0) for m in intel_list]  # placeholder for logging
        else:
            log(f"[aa_rank] ABORT Intel empty and no prev -> no write", file=sys.stderr)
    if not agentic_list:
        log(f"[aa_rank] GUARD skip write {COMBO_AGENTIC} empty -> keep DB {len(prev_agentic) if prev_agentic else 0}")
        if prev_agentic is not None:
            agentic_list = prev_agentic
            agentic_sorted = [(m, alias.get(m, m), 0) for m in agentic_list]
        else:
            log(f"[aa_rank] ABORT Agentic empty and no prev -> no write", file=sys.stderr)
    # hanya tulis jika ada isinya
    for name, lst in [(COMBO_INTEL, intel_list), (COMBO_AGENTIC, agentic_list)]:
        if not lst:
            log(f"[aa_rank] SKIP write {name} empty")
            continue
        cur.execute("UPDATE combos SET models = ? WHERE name = ?", (json.dumps(lst), name))
        if cur.rowcount == 0:
            cur.execute("INSERT INTO combos(name, models) VALUES(?, ?)", (name, json.dumps(lst)))
        log(f"[aa_rank] wrote {name} {len(lst)} models")
    if vision_pool is not None:
        if len(vision_pool) == 0:
            log(f"[aa_rank] GUARD skip write vision empty -> keep DB {len(prev_vision) if prev_vision else 0}")
        else:
            write_vision_adapter(conn, vision_pool)
    else:
        log(f"[aa_rank] vision_pool None -> skip")
    try:
        remap_state = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00","Z"), "source": source, "intel": len(intel_list), "agentic": len(agentic_list), "vision": len(vision_pool) if vision_pool else 0}
        cur.execute("INSERT INTO kv(scope,key,value) VALUES(?,?,?) ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value", (KV_REMAP_SCOPE, KV_REMAP_KEY, json.dumps(remap_state)))
    except Exception as e:
        log(f"[aa_rank] remap kv fail {e}")
    conn.commit()
    cur.execute("SELECT name, json_array_length(models) FROM combos WHERE name IN (?,?)", (COMBO_INTEL, COMBO_AGENTIC))
    for row in cur.fetchall():
        log(f"[aa_rank] verify {row[0]} len={row[1]}")
    if vision_pool is not None:
        row = conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
        if row:
            d = json.loads(row[0])
            log(f"[aa_rank] verify vision {d.get('capacityAdapter',{}).get('vision',{})}")
    try:
        import tg_notify
        msg = f"<b>AA Rank</b> {source} sync {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} — Intel {len(intel_list)} Agentic {len(agentic_list)} ver={ver} src={source}"
        if vision_pool is not None:
            msg += f" Vision {len(vision_pool)}"
        tg_notify.notify(msg)
    except Exception:
        pass
    conn.close()
    log(f"[aa_rank] done source={source}")
    return 0

def main():
    if "--fetch" in sys.argv:
        sys.exit(do_fetch())
    if "--remap" in sys.argv:
        use_cache = "--no-cache" not in sys.argv
        dry = "--dry" in sys.argv
        write = "--write" in sys.argv or "--cron" in sys.argv or not dry
        with_vision = True
        if "--no-vision" in sys.argv:
            with_vision = False
        if "--vision" in sys.argv:
            with_vision = True
        if "--cron" in sys.argv:
            dry = False
            write = True
            use_cache = True
        # --cron = remap via cache (0 hit API), --fetch = 1x/hari hit API
        sys.exit(do_remap(write=write, with_vision=with_vision, dry=dry, use_cache=use_cache))
    dry = "--dry" in sys.argv
    write = "--write" in sys.argv
    cron = "--cron" in sys.argv
    vision_only = "--vision" in sys.argv and "--with-vision" not in sys.argv and "--remap" not in sys.argv
    vision_with_combos = "--with-vision" in sys.argv
    if cron:
        log("[aa_rank] --cron deprecated -> remap via cache (0 API hit); use --fetch 1x/hari + --remap/--cron for remap")
        sys.exit(do_remap(write=True, with_vision=vision_with_combos, dry=False, use_cache=True))
    alias = load_alias()
    log(f"[aa_rank] alias {len(alias)} entries vision_only={vision_only} with_vision={vision_with_combos} (legacy path: add --fetch/--remap for cache mode)")
    if vision_only:
        conn, _ = _open_conn()
        try:
            rows, ver, _, _ = fetch_api_all()
            log(f"[aa_rank] API total {len(rows)} ver={ver}")
            intel_map = {}
            agentic_map = {}
            for r in rows:
                name = r.get("name", "").strip()
                ev = r.get("evaluations", {})
                intel = ev.get("artificial_analysis_intelligence_index")
                agentic = ev.get("artificial_analysis_agentic_index")
                if isinstance(intel, (int, float)):
                    intel_map[name] = float(intel)
                if isinstance(agentic, (int, float)):
                    agentic_map[name] = float(agentic)
            log(f"[aa_rank] intel_map {len(intel_map)} agentic_map {len(agentic_map)}")
        except RuntimeError as e:
            low = str(e).lower()
            if "429" in str(e) or "rate" in low:
                log(f"[aa_rank] API 429 -> fallback cache")
                rows2, ver2 = load_cache()
                if rows2:
                    rows, ver = rows2, ver2
                    intel_map = {}
                    agentic_map = {}
                    for r in rows:
                        n = (r.get("name") or "").strip()
                        ev = r.get("evaluations") or {}
                        it = ev.get("artificial_analysis_intelligence_index")
                        ag = ev.get("artificial_analysis_agentic_index")
                        if isinstance(it, (int, float)): intel_map[n] = float(it)
                        if isinstance(ag, (int, float)): agentic_map[n] = float(ag)
                    log(f"[aa_rank] fallback intel {len(intel_map)} agentic {len(agentic_map)}")
                else:
                    log("[aa_rank] API 429 + no cache -> fallback distinct 50.0")
                    intel_map = {}
                    agentic_map = {}
                    rows = []
                    ver = "cached"
                    for mid in distinct_models(conn):
                        label = alias.get(mid)
                        if label and label not in intel_map:
                            intel_map[label] = 50.0
                    log(f"[aa_rank] fallback intel_map {len(intel_map)} from distinct")
            else:
                raise
        vision_pool = build_vision_pool(conn, intel_map, agentic_map, alias)
        if dry and not write:
            log("\n[aa_rank] --dry --vision done (no DB write)")
            if vision_pool:
                log(f"[aa_rank] vision pool preview {len(vision_pool)}")
                for mid, label, score in vision_pool:
                    tag = "free" if is_free(mid) else "paid"
                    log(f"  {score:6.1f} [{tag:4}] {mid:45} <- {label}")
            conn.close()
            return
        if not write:
            log("\n[aa_rank] nothing to write (use --dry or --write)")
            conn.close()
            return
        import shutil, hashlib
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        _, db_path = _open_conn()
        bak = db_path + f".bak-aa-vision-{ts}"
        try:
            shutil.copy2(db_path, bak)
            md5 = hashlib.md5(open(bak, "rb").read()).hexdigest()[:8]
            log(f"[aa_rank] backup {bak} md5 {md5}")
        except Exception as e:
            log(f"[aa_rank] backup fail {e}", file=sys.stderr)
            sys.exit(2)
        ok = write_vision_adapter(conn, vision_pool)
        conn.commit()
        row = conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
        if row:
            d = json.loads(row[0])
            log(f"[aa_rank] verify vision {d.get('capacityAdapter',{}).get('vision',{})}")
        conn.close()
        log("[aa_rank] vision done")
        return
    # legacy combo path (tanpa cache flag) -> tetap support tapi fallback cache jika 429
    try:
        rows, ver, _, _ = fetch_api_all()
        log(f"[aa_rank] API total {len(rows)} ver={ver}")
    except RuntimeError as e:
        low = str(e).lower()
        if "429" in str(e) or "rate" in low or "limit" in low:
            log(f"[aa_rank] API 429 -> fallback cache DB")
            rows, ver = load_cache()
            if not rows:
                log("[aa_rank] no cache -> abort 429", file=sys.stderr)
                sys.exit(3)
            log(f"[aa_rank] fallback cache n={len(rows)} ver={ver}")
        else:
            raise
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
    log(f"[aa_rank] intel_map {len(intel_map)} agentic_map {len(agentic_map)}")
    conn, db_path = _open_conn()
    distinct = distinct_models(conn)
    log(f"[aa_rank] distinct models {len(distinct)}")
    for m in distinct:
        log(f"  - {m} -> {alias.get(m, '?')}")
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
    log(f"[aa_rank] scored Intel {len(intel_scored)} Agentic {len(agentic_scored)} (before probe)")
    intel_probed = filter_active_and_probe(conn, intel_scored)
    agentic_probed = filter_active_and_probe(conn, agentic_scored)
    log(f"[aa_rank] probed Intel {len(intel_probed)} Agentic {len(agentic_probed)} (pass=[ok,rate])")
    intel_sorted = dedup_by_provider(intel_probed)
    agentic_sorted = dedup_by_provider(agentic_probed)
    log(f"[aa_rank] dedup Intel {len(intel_sorted)} Agentic {len(agentic_sorted)} (keep max score among pass)")
    intel_list = [mid for mid, _, _ in intel_sorted]
    agentic_list = [mid for mid, _, _ in agentic_sorted]
    log(f"[aa_rank] final Intel {len(intel_list)} Agentic {len(agentic_list)} (union fallback MAX, pure AA -score tie free)")
    log("\n[aa_rank] Intelligence sorted (pure AA -score tie free, 1/provider):")
    for mid, label, score in intel_sorted:
        tag = "free" if is_free(mid) else "paid"
        log(f"  {score:6.1f} [{tag:4}] {mid:45} <- {label}")
    log("\n[aa_rank] Agentic sorted (pure AA -score tie free, 1/provider):")
    for mid, label, score in agentic_sorted:
        tag = "free" if is_free(mid) else "paid"
        log(f"  {score:6.1f} [{tag:4}] {mid:45} <- {label}")
    vision_pool = None
    if vision_with_combos:
        try:
            vision_pool = build_vision_pool(conn, intel_map, agentic_map, alias)
        except Exception as e:
            log(f"[aa_rank] vision pool fail {e}")
    if dry and not write:
        log("\n[aa_rank] --dry done (no DB write)")
        if vision_pool:
            log(f"[aa_rank] vision pool preview {len(vision_pool)}")
            for mid, label, score in vision_pool:
                tag = "free" if is_free(mid) else "paid"
                log(f"  {score:6.1f} [{tag:4}] {mid:45} <- {label}")
        return
    if not write:
        log("\n[aa_rank] nothing to write (use --dry or --write)")
        return
    import shutil, hashlib
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = db_path + f".bak-aa-{ts}"
    try:
        shutil.copy2(db_path, bak)
        md5 = hashlib.md5(open(bak, "rb").read()).hexdigest()[:8]
        log(f"[aa_rank] backup {bak} md5 {md5}")
    except Exception as e:
        log(f"[aa_rank] backup fail {e}", file=sys.stderr)
        sys.exit(2)
    cur = conn.cursor()
    # GUARD: jangan tulis combo kosong — pakai DB lama (fallback database)
    prev_intel = None
    prev_agentic = None
    prev_vision = None
    try:
        cur.execute("SELECT models FROM combos WHERE name=?", (COMBO_INTEL,))
        r = cur.fetchone()
        if r and r[0]:
            prev_intel = json.loads(r[0])
    except Exception:
        pass
    try:
        cur.execute("SELECT models FROM combos WHERE name=?", (COMBO_AGENTIC,))
        r = cur.fetchone()
        if r and r[0]:
            prev_agentic = json.loads(r[0])
    except Exception:
        pass
    try:
        row = conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
        if row and row[0]:
            d = json.loads(row[0])
            prev_vision = d.get("capacityAdapter", {}).get("vision", {}).get("models")
    except Exception:
        pass
    if not intel_list:
        log(f"[aa_rank] GUARD skip write {COMBO_INTEL} empty -> keep DB {len(prev_intel) if prev_intel else 0}")
        if prev_intel is not None:
            intel_list = prev_intel
            intel_sorted = [(m, alias.get(m, m), 0) for m in intel_list]  # placeholder for logging
        else:
            log(f"[aa_rank] ABORT Intel empty and no prev -> no write", file=sys.stderr)
    if not agentic_list:
        log(f"[aa_rank] GUARD skip write {COMBO_AGENTIC} empty -> keep DB {len(prev_agentic) if prev_agentic else 0}")
        if prev_agentic is not None:
            agentic_list = prev_agentic
            agentic_sorted = [(m, alias.get(m, m), 0) for m in agentic_list]
        else:
            log(f"[aa_rank] ABORT Agentic empty and no prev -> no write", file=sys.stderr)
    # hanya tulis jika ada isinya
    for name, lst in [(COMBO_INTEL, intel_list), (COMBO_AGENTIC, agentic_list)]:
        if not lst:
            log(f"[aa_rank] SKIP write {name} empty")
            continue
        cur.execute("UPDATE combos SET models = ? WHERE name = ?", (json.dumps(lst), name))
        if cur.rowcount == 0:
            cur.execute("INSERT INTO combos(name, models) VALUES(?, ?)", (name, json.dumps(lst)))
        log(f"[aa_rank] wrote {name} {len(lst)} models")
    if vision_pool is not None:
        if len(vision_pool) == 0:
            log(f"[aa_rank] GUARD skip write vision empty -> keep DB {len(prev_vision) if prev_vision else 0}")
        else:
            write_vision_adapter(conn, vision_pool)
    else:
        log(f"[aa_rank] vision_pool None -> skip")
    conn.commit()
    cur.execute("SELECT name, json_array_length(models) FROM combos WHERE name IN (?,?)", (COMBO_INTEL, COMBO_AGENTIC))
    for row in cur.fetchall():
        log(f"[aa_rank] verify {row[0]} len={row[1]}")
    if vision_pool is not None:
        row = conn.execute("SELECT data FROM settings WHERE id=1").fetchone()
        if row:
            d = json.loads(row[0])
            log(f"[aa_rank] verify vision {d.get('capacityAdapter',{}).get('vision',{})}")
    try:
        import tg_notify
        msg = f"<b>AA Rank</b> API v2/free sync {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')} — Intel {len(intel_list)} Agentic {len(agentic_list)} pureAA 1/provider ver={ver}"
        if vision_pool is not None:
            msg += f" Vision {len(vision_pool)}"
        tg_notify.notify(msg)
    except Exception:
        pass
    conn.close()
    log("[aa_rank] done")

if __name__ == "__main__":
    main()
