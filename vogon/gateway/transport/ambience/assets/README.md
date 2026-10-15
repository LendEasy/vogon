# Ambience assets

Drop real recordings here as WAV (any rate/width/channels — the loader decodes
to mono linear16 and resamples once at load):

- `room_tone*.wav` — one or more **seamless** office-floor loops, 30–60 s,
  recorded/edited very quiet (target ~−30 dBFS). Loop point must be click-free.
- `typing*.wav` — 3–5 short keyboard bursts, ~1 s each, dry (no room in them —
  the room bed supplies that).
- `oneshot_*.wav` (Phase 3) — sparse distant events (a far phone ring, a chair
  creak). **No voices / no chatter** — see the compliance boundary in
  docs/ambience-liveliness.md.

Until real files are present, the loader synthesises deterministic stand-ins
(filtered noise / click trains) so the feature runs and tests pass without
committing binary audio. Real assets supersede the synthetic source by presence
of a matching prefix.
