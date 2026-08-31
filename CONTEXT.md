# CONTEXT — 9RKM / 9Router Key Manager

Glossary istilah domain 9RKM. Tanpa detail implementasi.

## Istilah

**Key (providerConnection)** — satu kredensial provider di gateway 9Router (`providerConnections` row). "Key" = unit yang dinyalakan/dimatikan.

**Auto-OFF** — aksi menonaktifkan key (`isActive=0`) oleh scan thread karena gateway mencatat `errorCode` fresh (≤1 jam, tanpa sukses setelahnya). Bukan hukuman permanen — hanya isolasi sementara.

**Scan** — thread pembaca `requestDetails` tiap 5 detik untuk menemukan key kandidat Auto-OFF. Dipause selama remap.

**Siklus (cycle)** — periode 5 jam, epoch-aligned. Unit waktu untuk semua aksi cycle thread.

**Remap** — proses discovery kandidat model terbaik per provider (skor Artificial Analysis + katalog live), probe terbaik→terjelek sampai 2xx, tulis 1 model/provider/combo ke `Artificial-Analysis-Intelligence-Index` & `Artificial-Analysis-Agentic-Index`. Harus **side-effect-free saat gagal** (gagal = DB combo kembali seperti semula, tidak ada efek lain).

**Probe** — satu request uji (`Reply PONG only` / 1×1 PNG) ke satu model via gateway. 2xx = lolos. 401/402/403/404/429/timeout = kandidat turun, bukan provider dihukum.

**Reset** — mengaktifkan kembali SEMUA key + membersihkan state error gateway. Berjalan HANYA setelah remap sukses + E2E lolos (urutan keputusan ADR 0002).

**E2E** — probe kedua combo lewat jalur nyata gateway setelah remap. Gagal → rollback.

**Toggle** — saklar ON/OFF milik Dea untuk seluruh mekanisme (scan + reset + remap). OFF = semua aksi berhenti, bukan cuma scan.

**Kunci remap (remap lock)** — satu flock `/tmp/9rkm-remap.lock`, dipakai key_manager DAN aa_rank standalone. Tidak ada mekanisme lock kedua.

**OFF berulang (problem key)** — key yang Auto-OFF di ≥3 siklus 5-jam berbeda **sejak request sukses nyata terakhir**. Reset/ACTIVATE ALL tidak menghapus bukti ini (menyalakan key ≠ sembuh); hanya request sukses setelah auto-off terakhir yang menghapusnya. Status INFORMASI untuk investigasi akar masalah — bukan status terminal, tidak mengubah perlakuan key sama sekali.

~~Retire / pensiun~~ — konsep DIHAPUS (ADR 0001). Tidak ada key terminal.

## Aturan bahasa

- Semua key OFF diperlakukan setara (keputusan Dea 31 Agu): tidak ada perlakuan khusus manual vs auto. `manual_off_by` hanya jejak histori "OFF (bulk)".
- "Down" menjelaskan model/kandidat (probe gagal); "OFF" menjelaskan key. Provider tidak pernah "OFF", hanya key-nya.
