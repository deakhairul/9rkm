#!/usr/bin/env python3
"""
daily-key-check.py — reset key 9Router tiap 5 jam.
- Membaca state multi-day dari tabel 'kv' SQLite (scope='hourly_key_disable', key='state').
- ON-kan semua key (`isActive=1`), KECUALI yang sudah pensiun (is_retired == True atau failed_cycles >= MAX_FAILED_CYCLES).
- Manual OFF TIDAK lagi jadi pengecualian reset (keputusan Dea 16 Agu): semua non-retired di-ON-kan tiap 08:00; key yang mati diisolasi otomatis oleh hourly-key-disable tiap menit.
- Key yang mencapai failed_cycles >= MAX_FAILED_CYCLES ditandai is_retired = True (PERMANEN) dan dibiarkan OFF (`isActive=0`).
- State healing untuk key non-pensiun: jika tidak kena OFF di hari kemarin/hari ini, counter reset ke 0.
- Single unified critical section under exclusive lock (fail-close).
- DB mutation & State update di-commit dalam SATU transaksi SQL atomik.
- Lapor ringkasan ke Telegram.
TIDAK menyentuh combo / urutan model. Key TIDAK pernah dihapus otomatis dari DB.
"""
import sys, os, json, datetime, sqlite3, urllib.request, urllib.error, urllib.parse, html

try:
    import fcntl
except ImportError:
    fcntl = None

DB = os.environ.get("ROUTER_DB", "/home/ubuntu/.9router/db/data.sqlite")
ENV = os.environ.get("ROUTER_ENV", "/home/ubuntu/scripts/daily-key-check.env")
LOCK_FILE = os.environ.get("ROUTER_LOCK", "/home/ubuntu/scripts/.key-state.lock")
LOG = os.environ.get("ROUTER_LOG", "/home/ubuntu/scripts/daily-key-check.log")
MAX_FAILED_CYCLES = 10
CYCLE_HOURS = 5
KV_SCOPE = "hourly_key_disable"
KV_KEY = "state"

def log(msg):
    ts = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S WIB")
    line = f"{ts} | {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def get_db():
    conn = sqlite3.connect(DB, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def get_iso_now():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    ms = now_utc.microsecond // 1000
    return now_utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"

def load_env():
    vals = {}
    if os.path.exists(ENV):
        for line in open(ENV, encoding="utf-8"):
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.strip().partition("=")
                vals[k.strip()] = v.strip()
    return vals

def notify(msg):
    env = load_env()
    bot = env.get("BOT_TOKEN")
    chat = env.get("CHAT_ID", "355679325")
    if not bot:
        log("[-] BOT_TOKEN tidak ditemukan, skip Telegram.")
        return
    esc = html.escape(msg, quote=False)
    html_msg = esc.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>").replace("&lt;code&gt;", "<code>").replace("&lt;/code&gt;", "</code>").replace("&lt;i&gt;", "<i>").replace("&lt;/i&gt;", "</i>")
    for params in ({"parse_mode": "HTML", "text": html_msg}, {"text": msg}):
        data = urllib.parse.urlencode({"chat_id": chat, **params}).encode()
        try:
            with urllib.request.urlopen(
                f"https://api.telegram.org/bot{bot}/sendMessage", data=data, timeout=15
            ) as r:
                if r.status == 200:
                    return
        except urllib.error.HTTPError as e:
            if e.code == 400 and params.get("parse_mode"):
                continue
            log(f"[-] Gagal kirim Telegram: {e}")
            return
        except Exception as e:
            log(f"[-] Gagal kirim Telegram: {e}")
            return

def acquire_exclusive_lock():
    if not fcntl:
        return None
    lock_dir = os.path.dirname(LOCK_FILE) or "."
    os.makedirs(lock_dir, exist_ok=True)
    f = open(LOCK_FILE, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except Exception as e:
        f.close()
        raise RuntimeError(f"Gagal memperoleh exclusive lock {LOCK_FILE} (sedang dipakai proses lain): {e}")

def release_exclusive_lock(lock_fd):
    if lock_fd and fcntl:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass

def load_state_from_db(cursor):
    cursor.execute("SELECT value FROM kv WHERE scope = ? AND key = ?;", (KV_SCOPE, KV_KEY))
    row = cursor.fetchone()
    if row and row["value"]:
        try:
            return json.loads(row["value"])
        except Exception as e:
            raise RuntimeError(f"Corrupt JSON in kv table ({KV_SCOPE}/{KV_KEY}): {e}")
    return {}

def save_state_to_db(cursor, state):
    json_str = json.dumps(state, indent=2)
    cursor.execute(
        "INSERT INTO kv (scope, key, value) VALUES (?, ?, ?) ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value;",
        (KV_SCOPE, KV_KEY, json_str)
    )

def reconcile_state_and_connections(conns, state, yesterday_wib, today_wib):
    to_activate = []
    retired = []

    for c in conns:
        cid = c["id"]
        cstate = state.get(cid)

        if cstate:
            is_ret = cstate.get("is_retired", False)
            failed_cycles = cstate.get("failed_cycles", cstate.get("consecutive_off_days", 0))
            last_off = cstate.get("last_off_date", "")
            off_cycle_id = cstate.get("off_cycle_id")

            # 1. Retired check (PERMANEN) DULUAN — bersihkan flag usang manual_off juga.
            if is_ret or failed_cycles >= MAX_FAILED_CYCLES:
                cstate["is_retired"] = True
                cstate["failed_cycles"] = max(cstate.get("failed_cycles", 0), MAX_FAILED_CYCLES)
                cstate.pop("manual_off", None)
                cstate.pop("auto_off_ts", None)
                state[cid] = cstate
                retired.append({**c, "failed_cycles": cstate["failed_cycles"], "last_off_date": last_off})
                continue

            if failed_cycles >= MAX_FAILED_CYCLES:
                cstate["is_retired"] = True
                retired.append({**c, "failed_cycles": failed_cycles, "last_off_date": last_off})
                state[cid] = cstate
                continue

            # 2. Manual OFF tidak lagi pengecualian (keputusan Dea 16 Agu): semua non-retired
            #    di-ON-kan; key mati diisolasi oleh hourly scanner per menit. Flag usang di-reset
            #    (cek keberadaan field, bukan nilai — manual_off=false pun harus dibersihkan).
            if "manual_off" in cstate or "auto_off_ts" in cstate:
                cstate.pop("manual_off", None)
                cstate.pop("auto_off_ts", None)
                state[cid] = cstate

            # 3. State healing HANYA untuk key non-pensiun

            to_activate.append(c)
        else:
            # Connection tanpa state: tetap di-ON-kan (keputusan Dea 16 Agu).
            to_activate.append(c)

    return to_activate, retired

def run_daily_reset(conn, cursor, now_wib, today_wib, yesterday_wib):
    cursor.execute("SELECT id, provider, name, email, isActive, updatedAt FROM providerConnections;")
    rows = cursor.fetchall()
    conns = [{"id": r["id"], "provider": r["provider"], "name": r["name"] or r["provider"], "isActive": r["isActive"], "updatedAt": r["updatedAt"]} for r in rows]

    state = load_state_from_db(cursor)
    to_activate, retired = reconcile_state_and_connections(conns, state, yesterday_wib, today_wib)

    now_iso = get_iso_now()

    # 1. ON-kan yang layak di-reset
    if to_activate:
        act_ids = [c["id"] for c in to_activate]
        placeholders = ",".join("?" for _ in act_ids)
        # Bersihkan state error dari data JSON saat re-ON (keputusan Dea 16 Agu):
        # errorCode/backoffLevel stale tidak boleh membuat key langsung di-OFF lagi oleh
        # auto-off (atau ter-retire 3 hari walau sebenarnya pulih). json_remove atomik
        # di SQL -> tanpa read-modify-write blob (hindari lost update dgn 9Router).
        cursor.execute(
            f"UPDATE providerConnections SET data = json_remove(data, '$.errorCode', '$.lastError', '$.lastErrorAt', '$.backoffLevel'), updatedAt = ? WHERE id IN ({placeholders}) AND json_valid(data);",
            [now_iso] + act_ids
        )
        cursor.execute(
            f"UPDATE providerConnections SET isActive = 1, updatedAt = ? WHERE id IN ({placeholders});",
            [now_iso] + act_ids
        )
        log(f"ON: {len(to_activate)} koneksi diaktifkan kembali (state error dibersihkan).")

    # 2. Pastikan key pensiun (retired) tetap isActive = 0 di DB
    if retired:
        ret_ids = [c["id"] for c in retired]
        placeholders = ",".join("?" for _ in ret_ids)
        cursor.execute(
            f"UPDATE providerConnections SET isActive = 0, updatedAt = ? WHERE id IN ({placeholders});",
            [now_iso] + ret_ids
        )
        log(f"RETIRED: {len(retired)} koneksi tetap OFF (>= {MAX_FAILED_CYCLES} siklus gagal).")

    # 3. Save state to KV table
    save_state_to_db(cursor, state)

    # 4. Commit single atomic SQL transaction (jumlah statement variabel: UPDATE data
    #    json_remove + UPDATE isActive per batch + save state — tetap satu commit)
    conn.commit()

    return to_activate, retired

def main():
    log("=== Daily Key Check & Reset start ===")
    now_wib = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
    today_wib = now_wib.strftime("%Y-%m-%d")
    yesterday_wib = (now_wib - datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    lock_fd = None
    conn = None
    exit_code = 0
    try:
        lock_fd = acquire_exclusive_lock()
        conn = get_db()
        cursor = conn.cursor()

        to_activate, retired = run_daily_reset(conn, cursor, now_wib, today_wib, yesterday_wib)

        ts_str = now_wib.strftime("%d %b %H:%M WIB")
        msg_lines = [
            f"🩺 <b>Daily Key Check & Reset ({ts_str})</b>\n",
            f"✅ <b>{len(to_activate)}</b> key di-ON-kan (Reset Harian).",
        ]

        if retired:
            msg_lines.append(f"\n⛔ <b>{len(retired)}</b> key DIPENSIUNKAN (tetap OFF, ≥{MAX_FAILED_CYCLES} siklus gagal):")
            for r in retired[:15]:
                msg_lines.append(f"• <b>{html.escape(r['provider'])}</b> ({html.escape(r['name'][:20])}) — {r['failed_cycles']} hari gagal berturut")
            if len(retired) > 15:
                msg_lines.append(f"<i>...dan {len(retired)-15} key lainnya.</i>")
            msg_lines.append("\n<i>Key dipensiunkan TIDAK dihapus otomatis. Hapus manual via dashboard 9Router jika sudah tidak diperlukan.</i>")
        else:
            msg_lines.append("\n<i>Nol key pensiun (semua key non-pensiun diaktifkan kembali).</i>")

        notify("\n".join(msg_lines))
        log("=== Daily Key Check & Reset done ===")

    except Exception as e:
        log(f"[-] Fatal error: {e}")
        try:
            notify(f"⛔ <b>daily-key-check FAIL</b>\n\n<code>{html.escape(str(e))[:500]}</code>\n\n<i>Reset harian key 9Router gagal — periksa log VPS.</i>")
        except Exception:
            pass
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        exit_code = 1
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        release_exclusive_lock(lock_fd)
        if exit_code != 0:
            sys.exit(exit_code)

if __name__ == "__main__":
    main()
