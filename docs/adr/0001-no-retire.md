# 1. Hapus konsep retire (pensiun) key

- Status: Accepted
- Tanggal: 2026-08-31
- Keputusan: Dea

## Konteks

9RKM sebelumnya punya konsep retire: key gagal ≥50 siklus (10 hari 10 jam) ditandai `is_retired` dan "tetap OFF sampai aksi manual" (klaim README). Praktiknya 3 jalan keluar pensiunan: ACTIVATE ALL me-reset `is_retired=False` semua key, scan me-unretire saat ada satu sukses, dan reset 5 jam mengaktifkan semua key aktif. Jadi "permanen" tidak pernah benar-benar permanen — hanya loop OFF↔ON lambat dengan angka counter yang naik terus.

## Keputusan

Hapus total: `MAX_FAILED_CYCLES`, `is_retired`, cabang retire di reset/reconcile, dan dead-end-nya. Setiap siklus SEMUA key diuji ulang — semua OFF diperlakukan setara (keputusan lanjutan Dea 31 Agu: "anggap saja tidak ada manual off"), `manual_off` hanya jejak histori di KV untuk label "OFF (bulk)" saat masih aktif. Key yang Auto-OFF di ≥3 siklus berbeda sejak sukses nyata terakhir tampil di UI sebagai "OFF berulang" — murni sinyal investigasi akar masalah, bukan perubahan perlakuan.

**Semantik counter (patch 31 Agu sore):** `failed_cycles` = jumlah siklus 5 jam berbeda ketika key Auto-OFF sejak request sukses nyata terakhir. Naik maksimal 1× per siklus (dedup `counted_cycle_id`). Reset siklus & ACTIVATE ALL **mempertahankan** counter — menyalakan key bukan bukti sembuh. Hanya request sukses dengan timestamp > `auto_off_ts` terakhir yang me-reset counter (sukses = bukti sembuh). DEACTIVATE ALL tidak menambah counter.

## Konsekuensi

- Kode lebih kecil: satu fungsi `reconcile_state_and_connections` dihapus, `run_reset` disederhanakan.
- Key yang benar-benar mati (billing habis, kredensial expired) akan OFF tiap siklus lagi — biaya probe per siklus diterima; itu harga visibilitas akar masalah.
- State lama dengan `is_retired:true` di DB VPS tidak dimigrasi — field diabaikan oleh kode baru; hapus manual kalau mau bersih.
- UI stat "Pensiun" berganti "OFF berulang (≥3 siklus)" dari field `problem_count`.
