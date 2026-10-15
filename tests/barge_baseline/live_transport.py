"""Opt-in provider integration: real TTS/Flux/AEC/controller, simulated Twilio.

Run from the repository root with PYTHONPATH=voice-gateway:data-plane and
python tests/barge_baseline/live_transport.py (from voice-gateway/).
No phone call or data-plane response is made. Artifacts stay in --out.
"""
# ruff: noqa: E402 — its imports follow load_dotenv: settings read the env at import
import argparse
import array
import asyncio
import audioop
import base64
import hashlib
import html
import io
import json
import logging
import time
import wave
from collections import deque
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / 'data-plane/.env', override=False)

from deepgram.core.events import EventType

from vogon.gateway.audio.audio_recorder import AudioRecorder
from vogon.gateway.audio.audio_timeline import build_timeline_from_dir
from vogon.gateway.audio.event_publisher import AudioEventPublisher
from vogon.gateway.pipeline.tts import TTSClient
from vogon.gateway.settings import settings
from vogon.gateway.transport.twilio_transport import TwilioTransport

SILENCE = b'\xff' * 160
AGENT = ('Let me explain the available options for your appointment. '
         'We can arrange a morning visit or an afternoon visit later this week. '
         'The team will send a confirmation with the location and instructions. '
         'You can review those details and choose the time that works for you.')
SCENARIOS = {
    'backchannel': dict(text='Okay.', cut=False, reason='cooperative_backchannel'),
    'floor_request': dict(text='Wait, let me speak. I need to change the appointment to Friday.',
                         cut=True, reason='policy_floor_request'),
    'protected': dict(text='Wait, let me speak. I need to change the appointment to Friday.',
                      cut=False, protected=True, reason='protected_span'),
    'safety': dict(text='Stop calling me. Please remove my number from your list.',
                   cut=True, protected=True, reason='policy_breakthrough'),
    'echo_only': dict(text='', cut=False, echo=0.65, reason=None),
    'ambiguous': dict(text='Well, actually.', cut=None, reason=None),
}


def wav(path, pcm, channels=1):
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(channels)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(pcm)


async def synthesize(text):
    if not text:
        return b''
    tts = TTSClient(voice='aura-2-orion-en', encoding='mulaw', sample_rate=8000)
    try:
        await tts.open()
        return b''.join([chunk async for chunk, _ in tts.stream(text)])
    finally:
        await tts.close()


class Endpoint:
    """20 ms playout, 40 ms clear propagation; marks follow their audio."""
    def __init__(self):
        self.queue = deque()
        self.wire = []
        self.sent = bytearray()
        self.played = bytearray()
        self.discarded = bytearray()
        self.ids_played = []
        self.ids_discarded = []
        self.serial = 0
        self.pending_clears = deque()
        self.mark_sent = []
        self.mark_received = []

    async def send(self, raw):
        msg = json.loads(raw)
        row = dict(t=time.monotonic(), event=msg['event'])
        if msg['event'] == 'media':
            data = base64.b64decode(msg['media']['payload'])
            assert len(data) % 160 == 0, 'unaligned outbound media'
            self.sent.extend(data)
            row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            for offset in range(0, len(data), 160):
                self.serial += 1
                self.queue.append(('media', (self.serial, data[offset:offset+160])))
        elif msg['event'] == 'mark':
            row['name'] = msg['mark']['name']
            self.mark_sent.append(row['name'])
            self.queue.append(('mark', row['name']))
        elif msg['event'] == 'clear':
            self.pending_clears.append(time.monotonic() + .04)
        self.wire.append(row)

    async def mark(self, t, name, cleared):
        self.mark_received.append(name)
        self.wire.append(dict(t=time.monotonic(), event='mark_return', name=name, cleared=cleared))
        await t.handle_text_frame(dict(event='mark', mark=dict(name=name)))

    async def tick(self, t):
        while self.pending_clears and self.pending_clears[0] <= time.monotonic():
            self.pending_clears.popleft()
            dropped = 0
            while self.queue:
                kind, data = self.queue.popleft()
                if kind == 'mark':
                    await self.mark(t, data, True)
                else:
                    ident, chunk = data
                    self.discarded.extend(chunk)
                    self.ids_discarded.append(ident)
                    dropped += len(chunk)
            self.wire.append(dict(t=time.monotonic(), event='clear_applied', bytes=dropped))
        while self.queue and self.queue[0][0] == 'mark':
            await self.mark(t, self.queue.popleft()[1], False)
        if not self.queue:
            return SILENCE
        _, (ident, chunk) = self.queue.popleft()
        self.played.extend(chunk)
        self.ids_played.append(ident)
        return chunk


async def run(name, spec, out):
    directory = out / name
    directory.mkdir(parents=True)
    settings.recordings_path = str(directory)
    settings.call_logs_path = str(directory)
    caller = await asyncio.wait_for(synthesize(spec['text']), 45)
    wav(directory/'generated_caller.wav', audioop.ulaw2lin(caller, 2))
    endpoint = Endpoint()
    publisher = AudioEventPublisher(name, None)
    recorder = AudioRecorder(stores=[])
    cfg = SimpleNamespace(safety_phrases=('stop calling', 'remove my number'),
                          floor_request_phrases=('wait', 'let me speak'), semantic_judge=True)
    # No call clocks or application calls: only the production audio transport.
    t = TwilioTransport(None, endpoint.send, call_id=name, recorder=recorder,
                        start_event={'streamSid': 'MZ-live-harness'}, audio_event_sink=publisher.emit)
    t._safety_phrases, t._floor_phrases = cfg.safety_phrases, cfg.floor_request_phrases
    assert t._aec is not None, 'native AEC required for live validation'
    await t.start()
    origin = time.monotonic()
    recorder.set_stream_epoch(origin)
    stt_frames, raw_events = [], []
    original_send = t._stt.send_audio

    async def capture(chunk):
        stt_frames.append(chunk)
        await original_send(chunk)

    t._stt.send_audio = capture
    # Callbacks are left untouched; raw messages are captured at the same SDK seam.
    def trace(message):
        raw_events.append(dict(t=time.monotonic()-origin,
                               message=message.model_dump(mode='json') if hasattr(message, 'model_dump') else str(message)))
    t._stt._connection.on(EventType.MESSAGE, trace)
    player = asyncio.create_task(t.send_response(AGENT, 'agent-main', protected=spec.get('protected', False)))
    incoming, audible = bytearray(), bytearray()
    echo_history = deque([SILENCE]*9, maxlen=10)
    caller_start = None
    probes = []
    probe_started = False
    checks = []
    def check(label, passed, **evidence):
        checks.append(dict(name=label, passed=bool(passed), **evidence))
    try:
        for frame in range(1800):  # 36 second hard limit; no provider hangs hidden.
            await asyncio.sleep(max(0, origin + frame*.02-time.monotonic()))
            played = await endpoint.tick(t)
            if endpoint.ids_played and caller_start is None:
                caller_start = frame + 100  # overlap after 2 s of real playback
            index = (frame-caller_start)*160 if caller_start is not None else -1
            near = caller[index:index+160].ljust(160, b'\xff') if index >= 0 else SILENCE
            delayed = echo_history[0]
            echo_history.append(played)
            pcm = audioop.ulaw2lin(near, 2)
            if spec.get('echo'):
                pcm = audioop.add(pcm, audioop.mul(audioop.ulaw2lin(delayed, 2), 2, spec['echo']), 2)
            inbound = audioop.lin2ulaw(pcm, 2)
            incoming.extend(inbound)
            audible.extend(played)
            await t.handle_text_frame(dict(event='media', media=dict(
                payload=base64.b64encode(inbound).decode(), timestamp=str(frame*20))))
            if t._floor.caller_floor and not probe_started:
                probe_started = True
                probes.append(asyncio.create_task(t.send_response('This filler must be blocked.', 'blocked-filler')))
                probes.append(asyncio.create_task(t.send_response_guarded('This stale reply must be blocked.',
                                                                          'stale-reply', generation=-1)))
            caller_done = caller_start is not None and frame > caller_start + len(caller)/160 + 180
            if player.done() and not endpoint.queue and caller_done and not t._floor.caller_floor:
                break
        else:
            check('completes within 36 seconds', False)
        report = await asyncio.wait_for(player, 5)
        if probes:
            reports = await asyncio.wait_for(asyncio.gather(*probes), 3)
            check('filler and stale response send zero audio', all(r and r.chunks_sent == 0 for r in reports))
        await t.close()
        await publisher.close()
        events = json.loads((directory/f'{name}.audio_events.json').read_text())['events']
        published = [json.loads(line) for line in (directory/f'{name}.audio_events.jsonl').read_text().splitlines()]
        cuts = [e for e in events if e.get('action') == 'CUT']
        clears = [e for e in endpoint.wire if e['event'] == 'clear']
        check('expected cut behavior', spec['cut'] is None or bool(cuts) == spec['cut'], cuts=len(cuts))
        if spec['reason']:
            check('expected decision reason observed', any(e.get('reason') == spec['reason'] for e in events), reason=spec['reason'])
        check('every clear has a preceding cut', all(any(origin+c['t'] <= e['t']+.005 for c in cuts) for e in clears))
        # Artifact event timestamps are rounded to milliseconds.
        check('every cut sends clear within 60 ms', all(any(-.001 <= e['t']-(origin+c['t']) <= .06 for e in clears) for c in cuts))
        check('continuous STT receives every input frame', len(stt_frames) == frame+1, input_frames=frame+1, stt_frames=len(stt_frames))
        check('outbound audio conserved across playback and clear',
              sorted(endpoint.ids_played+endpoint.ids_discarded) == list(range(1, endpoint.serial+1)),
              sent_samples=len(endpoint.sent), played_samples=len(endpoint.played), cleared_samples=len(endpoint.discarded))
        check('all playback marks acknowledged exactly once', sorted(endpoint.mark_sent) == sorted(endpoint.mark_received))
        check('publication matches timeline events and timestamps', len(events) == len(published) and all(
            row['detail']['audio_seq'] == i+1 and
            dict(t=round(row['occurred_at']-origin, 3), **{k:v for k,v in row['detail'].items()
                 if k not in ('call_id','producer','audio_seq')}) == event
            for i, (row,event) in enumerate(zip(published, events, strict=False))))
        for cut in cuts:
            offset = int((cut['t']+.1)*8000)//160*160
            check('no audible agent tail beyond clear propagation',
                  audioop.rms(audioop.ulaw2lin(audible[offset:], 2), 2) == 0)
        if spec['text']:
            check('live Flux produced transcript', any(e['event'] in ('stt_interim','stt_final','caller_transcript') for e in events))
        if name == 'ambiguous':
            check('live semantic classifier requested', any(e['event'] == 'floor_classifier_requested' for e in events))
        (directory/f'{name}.wav').write_bytes(recorder.to_wav_bytes())
        with wave.open(io.BytesIO(recorder.to_wav_bytes())) as recording:
            recorded_agent = array.array('h', recording.readframes(recording.getnframes()))[1::2]
        for cut in cuts:
            check('timeline recording excludes cleared buffer tail',
                  all(abs(sample) <= 100 for sample in recorded_agent[int((cut['t']+.1)*8000):]))
        (directory/'timeline.html').write_text(build_timeline_from_dir(directory, name))
        wav(directory/'audible_agent.wav', audioop.ulaw2lin(audible, 2))
        wav(directory/'inbound.wav', audioop.ulaw2lin(incoming, 2))
        wav(directory/'stt_input.wav', audioop.ulaw2lin(b''.join(stt_frames), 2))
        # Separate actual endpoint playout from the transport's send-side recording.
        left, right = array.array('h', audioop.ulaw2lin(incoming, 2)), array.array('h', audioop.ulaw2lin(audible, 2))
        stereo = array.array('h', [0]) * (len(left)*2)
        stereo[0::2], stereo[1::2] = left, right
        wav(directory/'audible_stereo.wav', stereo.tobytes(), 2)
        (directory/'wire.json').write_text(json.dumps([dict(e, t=round(e['t']-origin, 4)) for e in endpoint.wire], indent=2))
        (directory/'provider_events.json').write_text(json.dumps(raw_events, indent=2))
        result = dict(scenario=name, passed=all(c['passed'] for c in checks), checks=checks,
                      delivery=vars(report), duration_s=round((frame+1)*.02, 2))
        (directory/'summary.json').write_text(json.dumps(result, indent=2, default=str))
        return result
    finally:
        if not player.done():
            player.cancel()
        await asyncio.gather(player, return_exceptions=True)
        if not publisher.closed:
            await t.close()
            await publisher.close()


def write_index(out, results):
    rows = []
    for result in results:
        name = html.escape(result['scenario'])
        status = 'PASS' if result['passed'] else 'FAIL'
        rows.append(f'<tr><td>{name}</td><td>{status}</td><td>'
                    f'<a href="{name}/timeline.html">Timeline</a> · '
                    f'<a href="{name}/audible_stereo.wav">Actual simulated audio</a> · '
                    f'<a href="{name}/stt_input.wav">STT input</a> · '
                    f'<a href="{name}/summary.json">Checks</a></td></tr>')
    (out/'index.html').write_text(
        '<!doctype html><meta charset="utf-8"><title>Live barge-in validation</title>'
        '<style>body{font:16px system-ui;max-width:1100px;margin:40px auto}'
        'td{padding:12px;border-bottom:1px solid #ddd}</style>'
        '<h1>Live barge-in validation</h1>'
        '<p>Real TTS, Flux STT, AEC and gateway controller. Simulated Twilio playback '
        'with 40 ms clear propagation; no telephone call or data-plane planner.</p>'
        '<p>Timeline audio estimates playback and trims at clear dispatch. '
        'Actual simulated audio includes clear propagation. Caller is left; agent is right.</p>'
        '<table><tr><th>Scenario</th><th>Result</th><th>Artifacts</th></tr>'
        + ''.join(rows) + '</table>')


async def main(args):
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    for key, value in dict(barge_controller_enabled=True, semantic_barge_enabled=True,
                           stt_feed_mode='open', barge_in_frame_log=True,
                           ambience_enabled=False, tts_phone_encoding='mulaw',
                           tts_phone_sample_rate=8000, tts_pacing_mode='sentence',
                           tts_buffer_lead_ms=500, barge_in_rms_threshold=1200,
                           barge_in_consecutive_frames=3, barge_in_grace_ms=100,
                           barge_in_adaptive_enabled=False, barge_in_confirm='off').items():
        setattr(settings, key, value)
    results = []
    for name in args.scenarios.split(','):
        print('LIVE '+name, flush=True)
        result = await run(name, SCENARIOS[name], out)
        results.append(result)
        (out/'summary.json').write_text(json.dumps(results, indent=2, default=str))
        write_index(out, results)
        print(json.dumps(result, default=str), flush=True)
    return int(not all(r['passed'] for r in results))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', default='/tmp/barge-transport-live')
    parser.add_argument('--scenarios', default=','.join(SCENARIOS))
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
