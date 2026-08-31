# 4. 9RKM v2: pisahkan Desired / Routing / Health; hapus Auto-OFF & reset-all

- Status: Accepted
- Tanggal: 2026-09-01
- Keputusan: Dea (grill-with-docs 34 keputusan, sesi 31 Agu–1 Sep)
- Supersedes: ADR 0001 (bagian "semua OFF setara"), ADR 0002 (urutan reset-all)

## Konteks

Inciden 31 Agu: sesudah reset, 124/166 key dimatikan scan dalam ±2 menit karena satu `errorCode` fresh — 404 model, 429 rate, 502/503 upstream — semuanya dianggap bukti key mati. `isActive` memikul 5 makna sekaligus (intent operator, routing, health, quarantine, recovery), sehingga: manual disable terhapus oleh reset, provider all-OFF hilang dari discovery, dan tidak ada cara jujur mengatakan key "aktif tapi belum terbukti sehat".

## Keputusan

1. **Tiga dimensi kanonik**: Desired State (operator), Routing State (engine; proyeksi tunggal `isActive`), Key Health (`Unknown/Recovering/Healthy/Unhealthy` + Reason + retryAt).
2. **Hapus Auto-OFF satu-error dan reset-all buta.** Key hanya jadi `Unhealthy` lewat error EKSPLISIT account/kredensial; error ambigu = observasi saja. Pemulihan hanya via canary exact-key → lease → sukses produksi.
3. **Manual Desired Disabled permanen** — engine tidak pernah menyalakannya (mengganti "semua OFF setara" ADR 0001).
4. **Blast-radius breaker** membekukan mutasi massal; Provider Incident tidak mencemari Key Health.
5. **Model Health tetap milik 9Router** (`modelLock`/fallback) — 9RKM tidak menduplikasi.
6. Penyimpanan: tabel namespaced `rkm_*` di SQLite 9Router; migrasi bootstrap semua key `Desired=Enabled, Health=Unknown`, routing diproyeksikan dari `isActive` saat cutover; counter legacy hanya audit.

## Konsekuensi

- Legacy toggle OFF menjadi langkah pertama (freeze mutasi) sampai Key Health Engine enforce.
- Web UI + test + README harus menyatakan tiga dimensi; "Aktif" tidak lagi berarti sehat.
- Biaya canary per provider diterima; provider tanpa canary = Unsupported Canary, routing terbatas manual.

## Rollout

Legacy OFF → backup → schema+bootstrap → **shadow 24 jam** (event `shadow_*`, nol mutasi) → canary registry provider kritis → pilot → cutover engine per engine → hapus writer legacy → soak.
