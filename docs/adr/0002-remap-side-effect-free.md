# 2. Remap side-effect-free: reset key berjalan SETELAH remap sukses

- Status: Accepted
- Tanggal: 2026-08-31

## Konteks

`_run_remap` lama membuka dengan `run_reset()` — mengaktifkan semua key + menghapus state error gateway — sebelum discovery/probe jalan. Kalau remap gagal di tengah (aa_rank exit ≠0, pm2 restart gagal, E2E gagal), hanya combo yang di-rollback; efek reset tetap terjadi. DB berubah padahal siklus dinyatakan gagal; jendela "key aktif tapi combo lama" juga terbuka selama menit-menit probing. Urutan ini warisan waktu remap dan reset masih dua hal terpisah (cron `aa_rank --remap` + reset thread).

## Keputusan

Balik urutan: remap (snapshot → aa_rank → restart → E2E) dulu; `run_reset()` baru setelah semua lolos. Gagal di titik mana pun = DB combo dikembalikan, restart ulang, tidak ada key yang tersentuh. Toggle OFF kini juga skip remap sepenuhnya (dulu: loop retry 300s selamanya).

## Konsekuensi

- Gagal remap = nol efek samping; state key persis seperti sebelum siklus.
- Reset kini hanya terjadi bersamaan dengan remap sukses — tidak ada lagi "reset tanpa remap" (setelah cron remap retired 31 Agu, memang tidak pernah ada jalur lain).
- E2E berjalan dengan key dalam kondisi pra-siklus — kandidat yang butuh reset key untuk lolos akan gagal siklus ini dan lolos siklus berikutnya. Trade-off diterima: benar > cepat satu siklus.
