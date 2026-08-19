# 9ROUTER — Supporting suite (custom milik kita)
# Engine = npm package decolua/9router (upstream, JANGAN diubah/di-fork).
# Repo ini HANYA berisi kode/supporting custom. Secret TIDAK PERNAH masuk repo.

## Isi
- `key_manager.py` — 9RKM key-manager (auto-off token, reset, kontrol TG/Web)
- `9rkm-index.html` — Web UI 9RKM (port 8819, Tailscale-only)
- `9rkm.service` — unit systemd `/etc/systemd/system/9rkm.service`
- `healthcheck-9router.sh` — watchdog port 20128, auto-restore via restart-9router.sh
- `gateway-quality-monitor.py` — sensor kualitas gateway (error-rate + canary)
- `health-models.py` — cek kesehatan model
- `9r-abort-monitor.py` — monitor abort/TOKEN_REFRESH dari log 9router
- `daily-key-check.py` — cek harian key

## Di luar repo (runtime, TIDAK di-commit)
- `.9router/db/data.sqlite` — provider + keys + traffic (46M, VPS)
- `jwt-secret`, `auth/` — auth VPS
- `.env*` apapun — secret (env path dijaga mode 600)

## Deploy note
- Semua script baca env dari file external (`/home/ubuntu/charon/.env` utk Telegram, BOT_TOKEN di env process)
- Jangan pernah hardcode secret di file (cek: `grep -rnE 'sk-|API_KEY|SECRET' .`)