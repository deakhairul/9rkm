#!/usr/bin/env python3
"""Shared Telegram notifier for the 9Router supporting suite.

Credentials are read from an external env file (never hardcoded, never committed).
Set TG_ENV to point at it; keys accepted: BOT_TOKEN/TELEGRAM_BOT_TOKEN and
CHAT_ID/TELEGRAM_CHAT_ID. Messages may contain <b>/<code>/<i> HTML tags.
"""
import html
import os
import urllib.parse
import urllib.request

ENV = os.environ.get("TG_ENV", "/home/ubuntu/scripts/daily-key-check.env")


def _load_env():
    vals = {}
    if os.path.exists(ENV):
        with open(ENV, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.strip().partition("=")
                    vals[k.strip()] = v.strip()
    return vals


def notify(msg):
    env = _load_env()
    bot = env.get("BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
    chat = env.get("CHAT_ID") or env.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID")
    if not bot or not chat:
        return False
    esc = html.escape(msg, quote=False)
    for tag in ("b", "code", "i"):
        esc = esc.replace(f"&lt;{tag}&gt;", f"<{tag}>").replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    for params in ({"parse_mode": "HTML", "text": esc}, {"text": msg}):
        data = urllib.parse.urlencode({"chat_id": chat, **params}).encode()
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{bot}/sendMessage", data=data, timeout=10) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
    return False
