# 3. Satu mekanisme lock remap (flock) + probe vision tanpa mutasi settings

- Status: Accepted
- Tanggal: 2026-08-31

## Konteks

Dua bug berbeda di jalur remap:

1. **Lock ganda di file sama.** `key_manager.py` pakai `flock` di `/tmp/9rkm-remap.lock`; `aa_rank.py` standalone pakai `O_EXCL` create/delete di file yang sama. File flock selalu ada di disk → aa_rank standalone selalu menganggap "locked"; dan setelah 1800s ia MENGHAPUS file yang sedang di-flock proses lain → window dua remap jalan bersamaan pada DB SQLite yang sama.

2. **Probe vision memutasi settings gateway.** `filter_vision_native` men-disable `capacityAdapter.vision` di tabel `settings` selama probe lalu restore. Proses mati di tengah = adapter tetap OFF diam-diam (self-heal hanya kalau proses hidup). Plus probe vision menjadikan HTTP 400 `bad_request` sebagai `ok` (ternary `"down" if X else "down"` — kedua cabang `down`, tapi return `ok` di jalur sebelumnya).

## Keputusan

1. aa_rank standalone ganti `flock` LOCK_EX|LOCK_NB — identik mekanisme key_manager, tidak pernah menghapus file lock, tidak ada lagi dua mekanisme di satu path. Stale-check 1800s dihapus (flock release otomatis saat proses mati — itulah gunanya flock).

2. Mutasi settings dihapus: probe vision jalan apa adanya (kandidat yang cuma bisa jalan via pool adapter akan ter-exclude — jujur, karena pool memang bukan "native vision"). Restore/remove settings hanya terjadi di jalur tulis yang punya backup+rollback sendiri.

3. Probe vision: `bad_request` = `down` (gagal kandidat) — konsisten dengan probe chat. Ternary broken dihapus.

## Konsekuensi

- Standalone `aa_rank --remap` kini benar-benar bisa jalan (dulu selalu SKIP) dan aman bersama key_manager — terutama penting saat dry-run manual saat daemon jalan.
- Vision pool bisa sedikit menyusut (kandidat adapter-only tidak lolos) — sesuai kontrak "native vision pool" dari awal.
- `is_free` string heuristic diketatkan word-boundary; sumber utama tetap field `pricing` di katalog (`model_is_free`).
