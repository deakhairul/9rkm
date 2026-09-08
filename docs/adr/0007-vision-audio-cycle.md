# 7. Vision+Audio masuk siklus 5 jam; version-watch lebur; COMBO_NAMES live

- Status: Accepted
- Tanggal: 2026-09-08
- Keputusan: Dea

## Konteks

1. Live `combos` di DB hanya `Builder`/`Planner`, tapi `COMBO_NAMES = ("Artificial-Analysis-Intelligence-Index",)`:
   `_probe_combo` menembak nama yang tidak ada → tiap siklus `combo E2E failed` → rollback
   (snapshot pun hanya mencakup nama basi itu, Builder/Planner tak ikut di-restore).
2. Vision writer ada (`capacityAdapter.vision`) tapi live selalu `--no-vision` → tak pernah jalan.
3. Audio nol kode; skema live ternyata `capacityAdapter.audioInput` (bukan `audio`),
   plus `pdf`/`videoInput` (default gateway: vision ON, audioInput ON, pdf/videoInput OFF).
4. Version-watch per-jam (`page=1`) boros kuota AA dan terpisah dari siklus.

## Keputusan

1. `aa_rank --remap` default mencakup vision+audio; opt-out via `--no-vision`/`--no-audio`.
   Ranking audio memakai skor Intel sebagai proxy (AA tak punya indeks audio),
   probe `input_audio` WAV sunyi 0.1s (spike 2026-09-08: OK di Gemini).
2. `COMBO_NAMES = ("Builder", "Planner")`; E2E combo tetap blokir, E2E vision/audio
   non-blokir (warning) supaya modalitas down tidak me-rollback combo sehat.
3. Rollback mencakup `settings.id=1` (vision+audioInput), bukan cuma `combos`.
4. Scheduler hanya siklus 5 jam; `ver` dibaca dari fetch remap (`kv aa_remap/state.ver`)
   dan disinkron ke `aa_version/state` tiap remap sukses. Fungsi cek per-jam dipertahankan
   sebagai deprecated, tidak dipanggil.
5. Guard pool-kosong jangan tulis berlaku untuk vision dan audio.

## Konsekuensi

- Satu fetch AA per siklus; reaksi versi baru lambat s.d. 5 jam (disetujui Dea).
- Pool vision/audio ditulis tiap siklus (roundRobin True); toggle dashboard gateway
   tetap otoritas tampilan, 9RKM otoritas isi pool.
- Status `/api/status` + Web UI menampilkan `Intel/Vision/Audio`.
