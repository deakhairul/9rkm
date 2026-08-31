# CONTEXT — 9RKM / 9Router Key Manager

Glossary istilah domain 9RKM. Tanpa detail implementasi.

## Istilah

**Key (providerConnection)** — satu kredensial provider di gateway 9Router (`providerConnections` row). "Key" = unit yang dinyalakan/dimatikan.

**Desired State** — kehendak operator (Dea) atas satu key: `Enabled` / `Disabled`. Tidak pernah diubah otomatis oleh engine. Manual `Disabled` bertahan sampai dibatalkan Dea; tidak ada mekanisme otomatis yang boleh menyalakannya (ADR 0004).

**Routing State** — apakah key boleh menerima traffic: `Enabled` / `Disabled`. Satu-satunya penulis adalah Key Health Engine, diproyeksikan ke `providerConnections.isActive`. Tidak ada writer lain (legacy writer dihapus saat cutover penuh).

**Key Health** — bukti kesehatan KREDENSIAL (bukan model): `Unknown` (belum teruji) / `Recovering` (canary lolos, menunggu sukses produksi) / `Healthy` (sukses produksi nyata) / `Unhealthy` (bukti eksplisit account/kredensial rusak + `retryAt`).

**Health Reason** — kategori akar `Unhealthy`: `Auth` / `Billing` / `Quota` / `Provider` / `Unknown`.

**Error eksplisit** — bukti langsung account/kredensial bermasalah (pesan body menyebut invalid key, service account deleted, credits habis, payment required, dll). Boleh langsung menurunkan Key Health (dalam batch yang lolos breaker).

**Error ambigu** — error yang TIDAK membuktikan key rusak: model down, 404 model, 429 rate-limit model, 5xx upstream, 400 request. Tidak pernah langsung mematikan key; hanya observasi (Model Health milik 9Router).

**Canary** — uji kredensial terhadap endpoint account provider (mis. `GET /models`) memakai key TERTENTU, tanpa inference bila mungkin. Lolos → key `Recovering` + lease routing terbatas. Registry per provider; provider tanpa canary tepercaya = `Unsupported Canary` (hanya routing terbatas manual).

**Lease Recovering** — hak routing sementara setelah canary: TTL 15 menit, budget 3 request. Habis tanpa sukses produksi → kembali `Unhealthy` + backoff.

**Sukses produksi** — request `success` via connectionId sama pada generation recovery terkini. Satu-satunya jalan ke `Healthy` dan satu-satunya yang menghapus failure streak.

**Retry At** — waktu paling cepat key `Unhealthy` boleh dicoba canary lagi. Sumber: header/body provider (reset kuota) atau exponential backoff + jitter (1 menit–5 jam).

**Blast-radius breaker** — freeze mutasi Auto-OFF baru saat ≥20% key global atau ≥50% key satu provider jadi kandidat sakit dalam rolling 60 detik (guard minimum 3 global / 2 provider). Lepas setelah 5 menit tanpa lonjakan + canary pulih. Batch pemicu freeze tidak dimutasi.

**Provider Incident** — kegagalan massal tingkat provider (domain `incidentDomain`). TIDAK mencemari Key Health individual; efeknya hanya: provider tidak eligible combo + retry key provider itu dipause. Status `Open` / `Recovering` / `Closed`.

**Siklus (cycle)** — periode 5 jam, epoch-aligned. Unit waktu rekonsiliasi Combo Remapper.

**Combo Remapper** — susun combo dari kandidat model provider yang punya ≥1 key Desired+Routing Enabled & Healthy, tanpa Provider Incident. Event-driven + rekonsiliasi 5 jam. Penyusutan massal dibekukan breaker combo; zero candidate/E2E gagal mempertahankan last-known-good.

**Vision Remapper** — lifecycle vision pool terpisah (harian + event). Status jujur: jumlah aktual tersimpan + `lastChecked`, nilai `Unknown/Verified/Unsupported/Error` — tidak pernah "0" saat hanya dilewati.

**Model Health** — kesehatan model/route. MILIK 9Router (`modelLock`, fallback). 9RKM tidak pernah memutuskan model mati; kandidat model gagal probe hanya turun ranking remap.

**Toggle / Engine toggle** — saklar per engine (Key Health / Combo / Vision) + kill-switch global milik Dea. OFF = engine itu tidak memutasi apa pun.

**Shadow mode** — engine berjalan penuh secara evaluasi tapi TIDAK memutasi routing/health produksi; semua keputusan dicatat sebagai event `shadow_*`. Pintu masuk setiap engine baru sebelum enforce.

**Kunci remap (remap lock)** — satu flock `/tmp/9rkm-remap.lock`, dipakai semua jalur remap. Tidak ada mekanisme lock kedua.

~~Retire / pensiun~~ — konsep DIHAPUS (ADR 0001). Tidak ada key terminal.

~~Auto-OFF~~ / ~~Reset~~ / ~~ACTIVATE ALL~~ — konsep v1 DISUPERSEDE (ADR 0004): satu error fresh apapun mematikan key, dan reset-all buta, diganti state-machine Key Health di atas.

## Aturan bahasa

- "Down" untuk model/kandidat (probe gagal); "OFF"/"Disabled" untuk routing; "Unhealthy" untuk bukti kredensial. Provider tidak pernah "OFF" — hanya routing/key-nya; provider punya "Incident".
- Key "Enabled" berarti boleh dirouting — BUKAN berarti sehat. Sehat = `Healthy` dengan bukti.
- Klaim status wajib menyebut dimensi: `Desired`, `Routing`, `Health` (contoh: "key X Desired=Enabled Routing=Disabled Health=Unhealthy reason=Billing").
