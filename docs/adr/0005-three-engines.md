# 5. Tiga engine terpisah: Key Health, Combo Remapper, Vision Remapper

- Status: Accepted
- Tanggal: 2026-09-01
- Keputusan: Dea

## Konteks

v1: satu cycle 5 jam menggabungkan remap combo + reset semua key; kegagalan remap punya efek samping state key; vision diproses `--no-vision` tapi status menulis `vision: 0` (data palsu — pool lama sebenarnya utuh).

## Keputusan

1. **Key Health Engine** — canary registry per provider, retryAt adaptif (header provider menang; fallback backoff+jitter 1 mnt–5 jam), lease 15 mnt/3 request, breaker, incident. Satu-satunya penulis Routing.
2. **Combo Remapper** — event-driven + rekonsiliasi 5 jam. Eligibility provider: ≥1 key Desired+Routing Enabled & Healthy, tanpa incident. Penyusutan massal = breaker combo; zero candidate/E2E gagal = pertahankan last-known-good; no-op tidak restart 9Router.
3. **Vision Remapper** — harian + event; status jujur (jumlah aktual + lastChecked; `Unknown/Verified/Unsupported/Error`).
4. Toggle per engine + kill-switch global di Web UI.

## Konsekuensi

- Kegagalan satu engine tidak memengaruhi dua lainnya.
- Discovery katalog = semua provider terkonfigurasi (bukan hanya yang punya key aktif saat itu) — menutup lubang provider terkunci keluar.
- Penjadwalan berbeda berarti event table `rkm_event` jadi sumber audit lintas engine.
