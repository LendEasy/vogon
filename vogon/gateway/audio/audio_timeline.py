"""Per-call audio timeline — one HTML page showing what happened on the wire.

Builds a fully self-contained page (audio as a base64 data-URI, data inlined,
zero external deps — the frontend's no-dependency philosophy) from the three
local per-call artifacts written at call end:

    <call_id>.wav                stereo recording  (L=caller, R=agent)
    <call_id>.frames.csv         Phase 0 per-20ms RMS + gate state  (optional)
    <call_id>.audio_events.json  sparse transport events            (optional)

Consumed by BOTH surfaces so there is exactly one renderer:
  - the application's route  GET /api/v1/calls/{id}/audio-timeline  (live serving)
  - proto/barge/render_call_timeline.py                          (offline CLI)

Lanes (top→bottom): caller/agent waveforms · RMS energy chart with the barge
threshold rule · agent activity (TTS/fillers/ambience) · caller speech (STT
markers + optional VAD shading) · barge-gate events · call clocks (silence/
duration ladder firings, the filler hold). A structured color KEY is rendered
under the chart. Click to seek, wheel to zoom, drag to pan; the playhead and
hover crosshair report their positions live. See docs/audio-timeline.md.
"""
from __future__ import annotations

import base64
import csv
import html
import json
import wave
from pathlib import Path

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


# ── Artifact loading ───────────────────────────────────────────────────────────

def load_frames_csv(path: Path) -> dict:
    """Columnar frame data for compact JSON embedding."""
    cols: dict[str, list] = {"t": [], "raw": [], "clean": [], "tts": [], "gap": [], "streak": [], "thr": []}
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            cols["t"].append(float(r["t_rel_s"]))
            cols["raw"].append(int(r["raw_rms"]))
            cols["clean"].append(int(r["clean_rms"]))
            cols["tts"].append(int(r["tts_playing"]))
            cols["gap"].append(int(r["gap_active"]))
            cols["streak"].append(int(r["streak"]))
            # Live adaptive threshold (added 2026-07; None on older captures).
            cols["thr"].append(int(r["threshold"]) if r.get("threshold") else None)
    if not any(v is not None for v in cols["thr"]):
        cols["thr"] = None
    return cols


def compute_vad_track(clean_wav: Path, mode: int = 2) -> list[int] | None:
    """0/1 speech flags per 20 ms frame of the post-AEC WAV (webrtcvad), or
    None when webrtcvad is unavailable. Aligns 1:1 with frames.csv rows."""
    try:
        import webrtcvad  # type: ignore[import]  # noqa: PLC0415 — optional dependency (ImportError -> None)
    except ImportError:
        return None
    vad = webrtcvad.Vad(mode)
    with wave.open(str(clean_wav), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
    out = []
    step = FRAME_SAMPLES * 2
    for i in range(len(pcm) // step):
        out.append(1 if vad.is_speech(pcm[i * step:(i + 1) * step], SAMPLE_RATE) else 0)
    return out


def build_timeline_from_dir(recordings_dir: Path, call_id: str, vad_mode: int = 2) -> str:
    """Assemble the page from whatever artifacts exist for ``call_id``.

    The stereo WAV is required (it is the timeline's clock); frames CSV,
    events JSON and the VAD track are each optional and degrade to an
    absent lane.
    """
    wav = recordings_dir / f"{call_id}.wav"
    if not wav.exists():
        raise FileNotFoundError(f"no recording for call {call_id!r}: {wav}")
    frames_csv = recordings_dir / f"{call_id}.frames.csv"
    events_json = recordings_dir / f"{call_id}.audio_events.json"
    clean_wav = recordings_dir / f"{call_id}.clean.wav"

    frames = load_frames_csv(frames_csv) if frames_csv.exists() else None
    events, meta = [], {}
    if events_json.exists():
        payload = json.loads(events_json.read_text())
        events, meta = payload.get("events", []), payload.get("meta", {})
    vad = compute_vad_track(clean_wav, vad_mode) if clean_wav.exists() else None

    return build_timeline_html(
        call_id, wav.read_bytes(), frames=frames, events=events, meta=meta, vad=vad,
    )


# ── Page builder ───────────────────────────────────────────────────────────────

def build_timeline_html(
    call_id: str,
    wav_bytes: bytes,
    frames: dict | None,
    events: list[dict],
    meta: dict,
    vad: list[int] | None = None,
) -> str:
    data = {
        "callId": call_id,
        "meta": meta,
        "events": events,
        "frames": frames,
        "vad": vad,
        "frameMs": FRAME_MS,
    }
    return (
        _PAGE
        .replace("__TITLE__", html.escape(f"Audio timeline — {call_id}"))
        .replace("__AUDIO_B64__", base64.b64encode(wav_bytes).decode("ascii"))
        .replace("__DATA_JSON__", json.dumps(data).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026'))
    )


_PAGE = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
  :root {
    --ink:#111827; --ink-soft:#374151; --ink-faint:#4b5563;
    --line:#94a3b8; --grid:#d8dee7; --bg:#ffffff;
    --caller:#1d4ed8;        /* caller: blue family        */
    --agent:#047857;         /* agent: green family        */
    --threshold:#dc2626;     /* limits/decisions: red      */
    --warn:#b45309;          /* attention: amber           */
    --filler:#7c3aed;        /* fillers/hold: violet       */
    --neutral:#475569;
  }
  body { background:var(--bg); color:var(--ink); font:13px/1.45 -apple-system,Segoe UI,sans-serif; margin:0; padding:16px; }
  h1 { font-size:16px; margin:0 0 4px; }
  .sub { color:var(--ink-faint); font-size:12px; margin-bottom:10px; }
  audio { width:100%; margin-bottom:8px; }
  #wrap { position:relative; user-select:none; }
  canvas { display:block; width:100%; background:var(--bg); border:1.5px solid var(--line); border-radius:6px; }
  #tip { position:absolute; display:none; background:#111827f0; color:#f9fafb; padding:5px 9px; border-radius:4px;
         font-size:11.5px; pointer-events:none; white-space:pre; z-index:5; box-shadow:0 2px 8px #0003; }
  #pos { display:inline-block; min-width:130px; font-variant-numeric:tabular-nums; font-weight:600;
         background:#eef2f7; border:1px solid var(--grid); border-radius:4px; padding:2px 8px; margin-left:8px; }
  .hint { color:var(--ink-faint); font-size:11.5px; margin:6px 0 2px; }

  /* ── Glossary / key ── */
  #key { margin-top:12px; border:1px solid var(--grid); border-radius:6px; padding:10px 12px; }
  #key h2 { font-size:12.5px; margin:0 0 8px; color:var(--ink); text-transform:uppercase; letter-spacing:.04em; }
  .keygrid { display:grid; grid-template-columns:repeat(auto-fill,minmax(310px,1fr)); gap:2px 24px; }
  .kgroup { margin:6px 0 2px; font-weight:700; color:var(--ink-soft); font-size:12px; grid-column:1/-1; }
  .krow { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--ink-soft); padding:1.5px 0; }
  .sw { flex:0 0 auto; width:22px; height:12px; border-radius:2.5px; border:1px solid #0002; }
  .sw.line { height:3px; border:none; }
  .sw.dash { height:0; border:none; border-top:3px dashed var(--threshold); }
  .sw.dot { width:12px; height:12px; border-radius:50%; }
  .sw.tri { width:0; height:0; border-left:7px solid transparent; border-right:7px solid transparent;
            border-bottom:12px solid var(--warn); border-top:none; border-radius:0; background:none; }
  #decisions { margin:16px 0; }
  #event-table { max-height:350px; overflow:auto; border:1px solid var(--grid); }
  table { border-collapse:collapse; width:100%; font-size:12px; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--grid); vertical-align:top; }
  th { position:sticky; top:0; background:var(--bg); }
  tbody tr { cursor:pointer; } tbody tr:hover { background:#eef2f7; }
  #event-filter { width:min(500px,70%); margin:8px; padding:6px; }
</style></head><body>
<h1>__TITLE__</h1>
<div class="sub" id="metaline"></div>
<audio id="player" controls src="data:audio/wav;base64,__AUDIO_B64__"></audio>
<div class="hint">Click the chart to seek &middot; scroll to zoom &middot; drag to pan &middot; hover anything for detail
  <span id="pos">&mdash;</span></div>
<div id="wrap"><canvas id="cv"></canvas><div id="tip"></div></div>
<section id="decisions">
  <h2>Speech and playback decisions</h2>
  <label>Filter <input id="event-filter" placeholder="Reason, episode, text, action or generation"></label>
  <button id="download-events">Download events</button>
  <span id="event-count"></span>
  <div id="event-table"><table><thead><tr>
    <th>Time</th><th>Event / action</th><th>Why</th><th>Episode / revision / generation</th><th>Evidence</th>
  </tr></thead><tbody id="event-rows"></tbody></table></div>
</section>

<div id="key">
  <h2>Key</h2>
  <div class="keygrid">
    <div class="kgroup">Waveforms (from the stereo recording)</div>
    <div class="krow"><span class="sw" style="background:var(--caller)"></span> Caller microphone (left channel)</div>
    <div class="krow"><span class="sw" style="background:var(--agent)"></span> Agent voice as sent (right channel)</div>

    <div class="kgroup">Signal energy (RMS, log scale &mdash; the barge-in detector&rsquo;s input)</div>
    <div class="krow"><span class="sw line" style="background:var(--caller)"></span> Raw inbound energy (mic + echo of the agent&rsquo;s own voice)</div>
    <div class="krow"><span class="sw line" style="background:var(--agent)"></span> Echo-cancelled energy (post-AEC &mdash; what the gate judges)</div>
    <div class="krow"><span class="sw dash"></span> Barge-in threshold (dashed red; moves when adaptive mode tracks the noise floor)</div>

    <div class="kgroup">Agent activity</div>
    <div class="krow"><span class="sw" style="background:var(--agent)"></span> Speech playout (TTS stream sent to the caller)</div>
    <div class="krow"><span class="sw" style="background:#04785766"></span> Buffer lead &mdash; sent but not yet heard (ends at Twilio&rsquo;s mark echo)</div>
    <div class="krow"><span class="sw" style="background:#e8590c"></span> Playout cancelled by a barge-in</div>
    <div class="krow"><span class="sw" style="background:var(--filler)"></span> Latency filler (&ldquo;Sure, one moment.&rdquo;) while a turn computes</div>
    <div class="krow"><span class="sw" style="background:#b4530966"></span> Think-gap ambience (typing under silence while the turn computes)</div>
    <div class="krow"><span class="sw" style="background:var(--warn); width:4px"></span> Typing one-shot start</div>

    <div class="kgroup">Caller speech (Deepgram STT)</div>
    <div class="krow"><span class="sw tri"></span> First interim transcript of a turn (speech first detected)</div>
    <div class="krow"><span class="sw dot" style="background:var(--ink)"></span> Final transcript (hover for text + confidence)</div>
    <div class="krow"><span class="sw" style="border:1.5px solid var(--warn); background:none; width:12px; height:12px"></span> Eager end-of-turn (speculative)</div>
    <div class="krow"><span class="sw" style="background:#04785733"></span> VAD speech shading (webrtcvad on the echo-cancelled audio)</div>

    <div class="kgroup">Barge-in gate</div>
    <div class="krow"><span class="sw" style="background:#dc262699; height:6px"></span> Energy streak (consecutive frames above threshold)</div>
    <div class="krow"><span class="sw dot" style="background:var(--neutral)"></span> Streak start</div>
    <div class="krow"><span class="sw dot" style="background:var(--warn)"></span> Suppressed by debounce (too short to count)</div>
    <div class="krow"><span class="sw dot" style="background:#e8590c"></span> Blocked by start-of-playout grace</div>
    <div class="krow"><span class="sw dot" style="background:var(--filler)"></span> Provisional window open (VAD deciding)</div>
    <div class="krow"><span class="sw dot" style="background:#7c3aed88"></span> Provisional aborted (no speech confirmed)</div>
    <div class="krow"><span class="sw dot" style="background:var(--warn)"></span> Energy candidate confirmed (semantic decision may still be pending)</div>
    <div class="krow"><span class="sw dot" style="background:#0e7490"></span> Twilio buffer cleared</div>
    <div class="kgroup">Floor decisions (hover for reason, episode and evidence)</div>
    <div class="krow"><span class="sw dot" style="background:var(--agent)"></span> Continue playback / allow caller turn</div>
    <div class="krow"><span class="sw dot" style="background:var(--warn)"></span> Gather more evidence / defer protected request</div>
    <div class="krow"><span class="sw dot" style="background:var(--threshold)"></span> Yield / cut playback</div>
    <div class="krow"><span class="sw dot" style="background:var(--filler)"></span> Reject stale result / block restart / suppress turn</div>
    <div class="krow"><span class="sw" style="background:#dc262622"></span> Caller owns the floor, until EOT and silence</div>

    <div class="kgroup">Call clocks</div>
    <div class="krow"><span class="sw" style="background:var(--warn); width:4px"></span> Silence ladder fired (warn / alert-probe / exit &mdash; hover for which)</div>
    <div class="krow"><span class="sw" style="background:var(--threshold); width:4px"></span> Duration limit fired</div>
    <div class="krow"><span class="sw" style="background:#7c3aed33"></span> Filler hold window (a turn is computing; fillers may play)</div>
  </div>
</div>

<script>
const D = __DATA_JSON__;
const player = document.getElementById('player');
const cv = document.getElementById('cv'), tip = document.getElementById('tip');
const pos = document.getElementById('pos');
const ctx = cv.getContext('2d');
const C = {                                   // palette (matches the CSS key)
  caller:'#1d4ed8', agent:'#047857', threshold:'#dc2626', warn:'#b45309',
  filler:'#7c3aed', neutral:'#475569', cancelled:'#e8590c', mark:'#0e7490',
  ink:'#111827', inkSoft:'#374151', inkFaint:'#4b5563', grid:'#d8dee7',
};

// ── layout ────────────────────────────────────────────────────────────────────
const LANES = [
  ['waveL', 64, 'caller mic'], ['waveR', 64, 'agent voice'],
  ['rms', 78, 'energy (RMS)'], ['agent', 46, 'agent activity'],
  ['caller', 40, 'caller speech'], ['barge', 46, 'barge gate'],
  ['floor', 48, 'floor decisions'],
  ['clocks', 30, 'call clocks'],
];
const PAD_L = 78, PAD_R = 8, GAP = 6;
const H = LANES.reduce((a, [,h]) => a + h + GAP, 8) + 18;

// duration: prefer decoded audio; fall back to data extents
let dur = 1;
function dataDur() {
  let m = 1;
  if (D.frames && D.frames.t.length) m = Math.max(m, D.frames.t[D.frames.t.length-1]);
  for (const e of D.events) m = Math.max(m, e.t);
  return m + 0.5;
}
dur = dataDur();

// view window (zoom/pan)
let v0 = 0, vSpan = dur;
const X = t => PAD_L + (t - v0) / vSpan * (cv.clientWidth - PAD_L - PAD_R);
const T = x => v0 + (x - PAD_L) / (cv.clientWidth - PAD_L - PAD_R) * vSpan;

// ── waveform peaks (decoded lazily) ───────────────────────────────────────────
let peaks = null;              // {L:[[min,max]...], R:[...]}  at PPS buckets/s
const PPS = 50;
(async () => {
  try {
    const b64 = player.src.split(',')[1];
    const bin = atob(b64), buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    const ac = new (window.AudioContext || window.webkitAudioContext)();
    const ab = await ac.decodeAudioData(buf.buffer);
    dur = Math.max(dur, ab.duration); if (vSpan >= dataDur()) { v0 = 0; vSpan = dur; }
    const mk = ch => {
      const d = ab.getChannelData(Math.min(ch, ab.numberOfChannels - 1));
      const n = Math.ceil(ab.duration * PPS), per = Math.floor(d.length / n) || 1, out = [];
      for (let i = 0; i < n; i++) {
        let mn = 0, mx = 0;
        for (let j = i * per; j < (i + 1) * per && j < d.length; j++) {
          if (d[j] < mn) mn = d[j]; if (d[j] > mx) mx = d[j];
        }
        out.push([mn, mx]);
      }
      return out;
    };
    peaks = { L: mk(0), R: mk(1) };
    ac.close();
  } catch (e) { console.warn('waveform decode failed', e); }
})();

// ── helpers ───────────────────────────────────────────────────────────────────
const evs = k => D.events.filter(e => e.event === k);
function spans(startK, endK) {           // pair start/end events into spans
  const out = []; let open = null;
  for (const e of D.events) {
    if (e.event === startK) open = e;
    else if (e.event === endK && open) { out.push([open, e]); open = null; }
  }
  if (open) out.push([open, { t: dur }]);
  return out;
}
const ttsSpans = spans('tts_start', 'tts_end');
const gapSpans = spans('gap_open', 'gap_close');
const holdSpans = spans('hold_open', 'hold_close');
const markEchoes = evs('mark_echo');
const fmtT = t => t.toFixed(2) + 's';

let hits = [];                            // tooltip hit boxes, rebuilt per frame
function mark(x, y, w, h, label) { hits.push({ x, y, w, h, label }); }
let hoverX = null;                        // crosshair (mouse position indicator)

// ── draw ──────────────────────────────────────────────────────────────────────
function draw() {
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth;
  if (cv.width !== W * dpr || cv.height !== H * dpr) {
    cv.width = W * dpr; cv.height = H * dpr; cv.style.height = H + 'px';
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  hits = [];
  let y = 8;

  // time grid
  const step = vSpan > 120 ? 30 : vSpan > 40 ? 10 : vSpan > 12 ? 5 : 1;
  ctx.strokeStyle = C.grid; ctx.fillStyle = C.inkFaint; ctx.font = '10px sans-serif';
  for (let t = Math.ceil(v0 / step) * step; t <= v0 + vSpan; t += step) {
    const x = X(t);
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H - 14); ctx.stroke();
    ctx.fillText(t + 's', x + 2, H - 4);
  }

  for (const [kind, h, label] of LANES) {
    ctx.fillStyle = C.inkSoft; ctx.font = '600 10.5px sans-serif';
    ctx.fillText(label, 4, y + 10);
    ctx.font = '10px sans-serif';
    if (kind === 'waveL' || kind === 'waveR') drawWave(kind === 'waveL' ? 'L' : 'R', y, h);
    else if (kind === 'rms') drawRms(y, h);
    else if (kind === 'agent') drawAgent(y, h);
    else if (kind === 'caller') drawCaller(y, h);
    else if (kind === 'barge') drawBarge(y, h);
    else if (kind === 'floor') drawFloor(y, h);
    else if (kind === 'clocks') drawClocks(y, h);
    y += h + GAP;
  }

  // hover crosshair (position indicator)
  if (hoverX !== null && hoverX >= PAD_L && hoverX <= W - PAD_R) {
    ctx.strokeStyle = '#11182755'; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(hoverX, 0); ctx.lineTo(hoverX, H - 14); ctx.stroke();
    ctx.setLineDash([]);
  }

  // playhead
  const px = X(player.currentTime);
  if (px >= PAD_L && px <= W - PAD_R) {
    ctx.strokeStyle = C.threshold; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H - 14); ctx.stroke(); ctx.lineWidth = 1;
    ctx.fillStyle = C.threshold;
    ctx.beginPath(); ctx.moveTo(px - 5, 0); ctx.lineTo(px + 5, 0); ctx.lineTo(px, 7); ctx.fill();
  }
  pos.textContent = '\u25b6 ' + fmtT(player.currentTime) +
      (hoverX !== null ? '   \u2316 ' + fmtT(Math.max(0, T(hoverX))) : '');
  requestAnimationFrame(draw);
}

function drawWave(ch, y, h) {
  if (!peaks) return;
  const mid = y + h / 2, p = peaks[ch];
  ctx.fillStyle = ch === 'L' ? C.caller : C.agent;
  const i0 = Math.max(0, Math.floor(v0 * PPS)), i1 = Math.min(p.length, Math.ceil((v0 + vSpan) * PPS));
  for (let i = i0; i < i1; i++) {
    const x = X(i / PPS);
    ctx.fillRect(x, mid + p[i][0] * (h / 2 - 2), Math.max(1, X((i + 1) / PPS) - x), Math.max(1, (p[i][1] - p[i][0]) * (h / 2 - 2)));
  }
}

const RMS_MAX = Math.log10(32768);
function rmsY(v, y, h) { return y + h - Math.max(0, Math.log10(Math.max(1, v))) / RMS_MAX * (h - 12); }
function drawRms(y, h) {
  if (!D.frames) { ctx.fillStyle = C.inkFaint; ctx.fillText('(no frames.csv — enable BARGE_IN_FRAME_LOG)', PAD_L + 6, y + h / 2); return; }
  const F = D.frames;
  if (F.thr) {                                   // LIVE adaptive threshold trajectory
    ctx.strokeStyle = C.threshold; ctx.setLineDash([4, 3]); ctx.lineWidth = 1.5;
    ctx.beginPath(); let st = false;
    for (let i = 0; i < F.t.length; i++) {
      if (F.thr[i] == null || F.t[i] < v0 - 1 || F.t[i] > v0 + vSpan + 1) continue;
      const x = X(F.t[i]), yy = rmsY(F.thr[i], y, h);
      st ? ctx.lineTo(x, yy) : (ctx.moveTo(x, yy), st = true);
    }
    ctx.stroke(); ctx.setLineDash([]); ctx.lineWidth = 1;
  } else if (D.meta.barge_in_rms_threshold) {    // static rule (older captures)
    const thr = D.meta.barge_in_rms_threshold;
    ctx.strokeStyle = C.threshold; ctx.setLineDash([4, 3]); ctx.lineWidth = 1.5;
    const ty = rmsY(thr, y, h);
    ctx.beginPath(); ctx.moveTo(PAD_L, ty); ctx.lineTo(cv.clientWidth - PAD_R, ty); ctx.stroke();
    ctx.setLineDash([]); ctx.lineWidth = 1;
    mark(PAD_L, ty - 4, cv.clientWidth - PAD_L - PAD_R, 8,
         'barge-in threshold = ' + thr + ' RMS (static)');
  }
  const line = (arr, color) => {
    ctx.strokeStyle = color; ctx.beginPath(); let started = false;
    for (let i = 0; i < F.t.length; i++) {
      if (F.t[i] < v0 - 1 || F.t[i] > v0 + vSpan + 1) continue;
      const x = X(F.t[i]), yy = rmsY(arr[i], y, h);
      started ? ctx.lineTo(x, yy) : (ctx.moveTo(x, yy), started = true);
    }
    ctx.stroke();
  };
  line(F.raw, C.caller); line(F.clean, C.agent);
}

function drawAgent(y, h) {
  for (const [a, b] of ttsSpans) {               // sent span
    const x0 = X(a.t), x1 = X(b.t);
    const isFiller = (a.audio_id || '').startsWith('filler-');
    ctx.fillStyle = b.cancelled ? C.cancelled : (isFiller ? C.filler : C.agent);
    ctx.fillRect(x0, y + 4, Math.max(2, x1 - x0), h - 14);
    mark(x0, y + 4, Math.max(6, x1 - x0), h - 14,
         (isFiller ? 'FILLER ' : 'TTS ') + (a.audio_id || '') +
         '\n"' + (a.text || '') + '"' + (b.cancelled ? '\nCANCELLED (barge)' : ''));
    const me = markEchoes.find(m => m.t >= b.t);   // buffer lead → heard-until
    if (me && !b.cancelled) {
      ctx.fillStyle = '#04785740';
      ctx.fillRect(x1, y + 4, Math.max(1, X(me.t) - x1), h - 14);
      mark(X(me.t) - 3, y, 7, h, 'mark_echo (caller finished hearing)\nbuffer lead ' + (me.t - b.t).toFixed(2) + 's');
      ctx.fillStyle = C.mark; ctx.fillRect(X(me.t) - 1, y + 2, 2, h - 10);
    }
  }
  for (const [a, b] of gapSpans) {               // think-gap fill
    ctx.fillStyle = '#b4530960';
    ctx.fillRect(X(a.t), y + h - 10, Math.max(2, X(b.t) - X(a.t)), 6);
  }
  for (const e of evs('typing_start')) {
    ctx.fillStyle = C.warn; ctx.fillRect(X(e.t), y + 2, 3, h - 10);
    mark(X(e.t) - 3, y, 9, h, 'typing_start (clip ' + e.clip + ')');
  }
  for (const e of evs('typing_stop')) { ctx.fillStyle = '#b4530988'; ctx.fillRect(X(e.t), y + 2, 1.5, h - 10); }
}

function drawCaller(y, h) {
  if (D.vad) {                                   // VAD speech shading
    ctx.fillStyle = '#04785733';
    const f = D.frameMs / 1000;
    let runStart = null;
    for (let i = 0; i <= D.vad.length; i++) {
      const on = i < D.vad.length && D.vad[i];
      if (on && runStart === null) runStart = i * f;
      if (!on && runStart !== null) { ctx.fillRect(X(runStart), y + 2, Math.max(1, X(i * f) - X(runStart)), h - 8); runStart = null; }
    }
  }
  for (const e of evs('stt_interim')) {
    ctx.fillStyle = C.warn; ctx.beginPath();
    ctx.moveTo(X(e.t), y + 4); ctx.lineTo(X(e.t) - 4, y + 12); ctx.lineTo(X(e.t) + 4, y + 12); ctx.fill();
    mark(X(e.t) - 4, y, 9, h, 'first interim\n"' + e.text + '"');
  }
  for (const e of evs('eager_eot')) {
    ctx.strokeStyle = C.warn; ctx.lineWidth = 1.5; ctx.strokeRect(X(e.t) - 3, y + 14, 6, 6); ctx.lineWidth = 1;
    mark(X(e.t) - 4, y + 12, 9, 10, 'eager EOT\n"' + e.text + '"');
  }
  for (const e of evs('stt_final')) {
    ctx.fillStyle = C.ink; ctx.beginPath(); ctx.arc(X(e.t), y + h - 12, 4, 0, 7); ctx.fill();
    mark(X(e.t) - 4, y + h - 16, 9, 9, 'STT final (conf ' + (e.confidence ?? 0).toFixed(2) + ')\n"' + e.text + '"');
  }
}

function drawBarge(y, h) {
  if (D.frames) {                                // streak intensity strip
    const F = D.frames; ctx.fillStyle = '#dc262699';
    for (let i = 0; i < F.t.length; i++) {
      if (!F.streak[i] || F.t[i] < v0 || F.t[i] > v0 + vSpan) continue;
      ctx.fillRect(X(F.t[i]), y + h - 8, Math.max(1, X(F.t[i] + D.frameMs / 1000) - X(F.t[i])), 5);
    }
  }
  const M = [
    ['barge_streak_start', C.neutral, e => 'streak start (rms ' + e.clean_rms + (e.threshold ? ', thr ' + e.threshold : '') + ')', 4],
    ['barge_suppressed', C.warn, e => 'SUPPRESSED by debounce\nstreak ' + e.streak + ', rms ' + e.clean_rms, 6],
    ['barge_grace_blocked', C.cancelled, e => 'blocked by start-grace\nstreak ' + e.streak, 6],
    ['barge_provisional_open', C.filler, e => 'PROVISIONAL open (VAD deciding)\nrms ' + e.clean_rms + ', thr ' + e.threshold, 7],
    ['barge_abort', '#7c3aed88', e => 'provisional ABORT (' + e.reason + ')\nVAD+ ' + e.vad_positive + '/' + e.window, 6],
    ['barge_confirmed', C.warn, e => e.confirm === 'local_vad'
        ? 'CANDIDATE CONFIRMED (VAD ' + e.vad_positive + '/' + e.window + ', ' + e.confirm_latency_ms + 'ms)\nrms ' + e.clean_rms
        : 'CANDIDATE CONFIRMED (' + (e.mode || e.confirm || '') + ')\nrms ' + e.clean_rms, 9],
    ['twilio_clear', C.mark, () => 'twilio clear (buffer flushed)', 5],
  ];
  for (const [k, color, fmt, r] of M) for (const e of evs(k)) {
    ctx.fillStyle = color; ctx.beginPath(); ctx.arc(X(e.t), y + 14, r / 1.6, 0, 7); ctx.fill();
    if (k === 'barge_confirmed') { ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(X(e.t), y + 2); ctx.lineTo(X(e.t), y + h - 10); ctx.stroke(); ctx.lineWidth = 1; }
    mark(X(e.t) - r, y + 14 - r, r * 2, r * 2, fmt(e));
  }
}

function floorColor(e) {
  if (e.action === 'DEFER' || e.action === 'GATHER_MORE') return C.warn;
  if (e.decision === 'YIELD' || e.event === 'barge_cut') return C.threshold;
  if (e.action === 'SUPPRESS_TURN' || /blocked|rejected|absorbed/.test(e.event)) return C.filler;
  if (e.decision === 'CONTINUE' || e.action === 'ALLOW_TURN' || e.event === 'floor_released') return C.agent;
  return C.warn;
}
function drawFloor(y, h) {
  let start = null;
  for (const e of D.events) {
    if ((e.event === 'floor_decision' && e.decision === 'YIELD' && e.action !== 'DEFER') || e.event === 'floor_acquired') start = e.t;
    if (e.event === 'floor_released' && start != null) {
      ctx.fillStyle = '#dc262622'; ctx.fillRect(X(start), y, X(e.t)-X(start), h-4); start = null;
    }
  }
  if (start != null) { ctx.fillStyle = '#dc262622'; ctx.fillRect(X(start), y, X(dur)-X(start), h-4); }
  const kinds = new Set(['floor_decision','floor_released','playback_blocked','playback_allowed',
    'floor_result_rejected','caller_transcript','barge_cut','barge_absorbed','barge_yield','barge_deferred_protected']);
  for (const e of D.events.filter(e => kinds.has(e.event))) {
    const x = X(e.t), color = floorColor(e);
    ctx.fillStyle = color; ctx.beginPath(); ctx.arc(x, y+18, 4, 0, 7); ctx.fill();
    if (e.action === 'CUT' || e.event === 'barge_cut') { ctx.fillRect(x-1,y,2,h-4); }
    mark(x-6,y+10,12,20,Object.entries(e).map(([k,v]) => k+': '+(typeof v === 'object' ? JSON.stringify(v) : v)).join('\n'));
  }
}

function drawClocks(y, h) {
  for (const [a, b] of holdSpans) {              // filler hold window
    ctx.fillStyle = '#7c3aed30';
    ctx.fillRect(X(a.t), y + 3, Math.max(2, X(b.t) - X(a.t)), h - 10);
    mark(X(a.t), y + 3, Math.max(6, X(b.t) - X(a.t)), h - 10,
         'filler hold (turn computing) ' + (b.t - a.t).toFixed(2) + 's' +
         (b.fillers_played != null ? '\nfillers played: ' + b.fillers_played + (b.interrupted ? ' — INTERRUPTED by caller' : '') : ''));
  }
  for (const e of evs('filler_start')) {
    ctx.fillStyle = C.filler; ctx.fillRect(X(e.t), y + 3, 3, h - 10);
    mark(X(e.t) - 3, y, 9, h, 'filler: "' + (e.text || '') + '"\n(' + (e.audio_id || '') + ')');
  }
  for (const e of evs('clock_silence')) {
    ctx.fillStyle = C.warn; ctx.fillRect(X(e.t), y + 3, 3.5, h - 10);
    ctx.fillStyle = C.warn; ctx.font = '9px sans-serif';
    ctx.fillText(e.threshold_id || '', X(e.t) + 4, y + 11); ctx.font = '10px sans-serif';
    mark(X(e.t) - 4, y, 11, h, 'silence ladder: ' + e.threshold_id + '\nidle ' + e.silent_for_sec + 's');
  }
  for (const e of evs('clock_duration')) {
    ctx.fillStyle = C.threshold; ctx.fillRect(X(e.t), y + 3, 3.5, h - 10);
    mark(X(e.t) - 4, y, 11, h, 'duration limit: ' + e.threshold_id + '\nelapsed ' + e.elapsed_sec + 's');
  }
}

// ── interaction ───────────────────────────────────────────────────────────────
let dragging = false, dragX = 0, dragV0 = 0, moved = false;
cv.addEventListener('mousedown', e => { dragging = true; moved = false; dragX = e.offsetX; dragV0 = v0; });
window.addEventListener('mouseup', () => dragging = false);
cv.addEventListener('mouseleave', () => { hoverX = null; tip.style.display = 'none'; });
cv.addEventListener('mousemove', e => {
  hoverX = e.offsetX;
  if (dragging) {
    const dt = (e.offsetX - dragX) / (cv.clientWidth - PAD_L - PAD_R) * vSpan;
    if (Math.abs(e.offsetX - dragX) > 3) moved = true;
    v0 = Math.max(0, Math.min(dur - vSpan, dragV0 - dt));
    return;
  }
  const hit = hits.find(hh => e.offsetX >= hh.x && e.offsetX <= hh.x + hh.w && e.offsetY >= hh.y && e.offsetY <= hh.y + hh.h);
  if (hit) { tip.style.display = 'block'; tip.style.left = (e.offsetX + 12) + 'px'; tip.style.top = (e.offsetY + 8) + 'px'; tip.textContent = hit.label + '\n@ ' + T(e.offsetX).toFixed(2) + 's'; }
  else tip.style.display = 'none';
});
cv.addEventListener('click', e => { if (!moved) player.currentTime = Math.max(0, T(e.offsetX)); });
cv.addEventListener('wheel', e => {
  e.preventDefault();
  const t = T(e.offsetX), f = e.deltaY > 0 ? 1.25 : 0.8;
  vSpan = Math.min(dur, Math.max(0.5, vSpan * f));
  v0 = Math.max(0, Math.min(dur - vSpan, t - (t - v0) * f));
}, { passive: false });

document.getElementById('metaline').textContent =
  'barge threshold=' + (D.meta.barge_in_rms_threshold ?? '?') + ' RMS \u00b7 debounce=' +
  (D.meta.barge_in_consecutive_frames ?? '?') + ' frames \u00b7 grace=' +
  (D.meta.barge_in_grace_ms ?? '?') + 'ms \u00b7 ambience=' + (D.meta.ambience_enabled ? 'on' : 'off') +
  ' \u00b7 ' + D.events.length + ' events' + (D.frames ? ' \u00b7 ' + D.frames.t.length + ' frames' : '') +
  (D.vad ? ' \u00b7 VAD track on' : '');

const decisionEvents = D.events.filter(e => /^(floor_|speech_|playback_|caller_transcript|audio_route|barge_|echo_|twilio_clear|outbound_queue_)/.test(e.event));
function renderEvents() {
  const query = document.getElementById('event-filter').value.toLowerCase();
  const rows = decisionEvents.filter(e => JSON.stringify(e).toLowerCase().includes(query));
  const body = document.getElementById('event-rows'); body.replaceChildren();
  for (const e of rows) {
    const tr = document.createElement('tr');
    const evidence = Object.fromEntries(Object.entries(e).filter(([k]) => !['t','event','reason','action','decision','episode_id','revision','generation'].includes(k)));
    for (const value of [e.t.toFixed(3)+'s', [e.event,e.decision,e.action].filter(Boolean).join(' / '),
        e.reason || e.verdict || e.source || '', [e.episode_id ?? '—',e.revision ?? '—',e.generation ?? '—'].join(' / '), JSON.stringify(evidence)]) {
      const td = document.createElement('td'); td.textContent = value; tr.appendChild(td);
    }
    tr.style.borderLeft = '3px solid '+floorColor(e);
    tr.onclick = () => { player.currentTime = Math.max(0,e.t); };
    body.appendChild(tr);
  }
  document.getElementById('event-count').textContent = rows.length+' events';
}
document.getElementById('event-filter').addEventListener('input',renderEvents);
document.getElementById('download-events').onclick = () => {
  const url = URL.createObjectURL(new Blob([JSON.stringify({meta:D.meta,events:D.events},null,2)],{type:'application/json'}));
  const a = document.createElement('a'); a.href=url; a.download=D.callId+'.audio_events.json'; a.click(); URL.revokeObjectURL(url);
};
renderEvents();
requestAnimationFrame(draw);
</script></body></html>
"""
