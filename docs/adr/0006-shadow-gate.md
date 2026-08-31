# 6. Shadow mode sebagai gerbang wajib setiap engine v2

- Status: Accepted
- Tanggal: 2026-09-01
- Keputusan: Dea

## Konteks

State-machine baru mengubah keputusan yang salah bisa mematikan hampir seluruh key produksi (bukti: insiden 124 key). Ensemble test lokal tidak cukup — perilaku nyata gateway (error fresh, timing, blast radius) hanya terlihat di traffic produksi.

## Keputusan

Setiap engine v2 wajib melewati **shadow ≥24 jam** sebelum enforce: evaluasi penuh, semua keputusan dicatat sebagai event `shadow_*`, NOL mutasi `providerConnections`/`combos`/`settings`. Enforce hanya aktif setelah review laporan shadow + pilot provider kritis.

## Konsekuensi

- `9rkm-shadow.service` (systemd terpisah) jadi komponen permanen — engine baru apa pun masuk lewat jalur yang sama.
- Kriteria naik stage: replay insiden 31 Agu di shadow memicu `shadow_breaker_would_freeze` dan `shadow_incident_candidate` dengan benar, tanpa satu pun mutasi produksi.
- Acceptance breaker diukur dari event shadow, bukan klaim.
