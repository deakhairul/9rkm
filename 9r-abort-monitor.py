#!/usr/bin/env python3
"""9Router abort monitor — tripwire: ERROR terminated / Fetch.onAborted (upstream socket mati)
dan TOKEN_REFRESH failed (kredensial). ⚡ DISCONNECT (client abort) TIDAK dipantau — normal.
Notif Telegram via kredensial env file (TG_ENV)."""
import os
import re
import sys

os.environ.setdefault("TG_ENV", "/home/ubuntu/auto-worker/.env")
from tg_notify import notify

LOG = "/home/ubuntu/.pm2/logs/9router-out.log"
OFFSET = "/tmp/9r-monitor-offset"
PAT = re.compile(r"ERROR: terminated|Fetch\.onAborted|TOKEN_REFRESH\] (All \d+ retry attempts failed|failed)", re.I)

def main():
    if not os.path.exists(LOG):
        print("log tidak ada")
        return
    last = 0
    if os.path.exists(OFFSET):
        try:
            last = int(open(OFFSET).read().strip())
        except Exception:
            last = 0
    size = os.path.getsize(LOG)
    if size < last:
        last = 0
    with open(LOG, "r", errors="replace") as f:
        if last:
            f.seek(last)
        tail = f.read()
        new_size = f.tell()
    open(OFFSET, "w").write(str(new_size))
    hits = [ln.strip()[:160] for ln in tail.splitlines() if PAT.search(ln)]
    if hits:
        msg = f"[9R-MONITOR] {len(hits)} kejadian upstream abort/token-fail di log 9Router:\n" + "\n".join(hits[:8])
        ok = notify(msg)
        print(f"alert: {len(hits)} kejadian; telegram sent={ok}")
    else:
        print("bersih, offset ->", new_size)

if __name__ == "__main__":
    main()
