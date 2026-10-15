"""Publish audio diagnostics separately from the conversational call log."""
import asyncio
import json
import logging
import time
from pathlib import Path

from vogon.gateway.settings import settings

logger = logging.getLogger(__name__)


class AudioEventPublisher:
    def __init__(self, call_id, redis):
        self.queue = asyncio.Queue()
        self.call_id = call_id
        self.serial = 0
        self.closed = False
        self.task = asyncio.create_task(self._write(redis))

    async def _write(self, redis):
        path = Path(settings.recordings_path) / f'{self.call_id}.audio_events.jsonl'
        key = f'call:{{{self.call_id}}}:audio'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            while True:
                line = await self.queue.get()
                if line is None:
                    return
                stream.write(line + '\n')
                stream.flush()
                if redis is not None:
                    try:
                        await redis.xadd(key, {'data': line}, maxlen=10000, approximate=True)
                        await redis.expire(key, 86400)
                    except Exception:
                        logger.warning('Audio stream publication failed: %s', self.call_id, exc_info=True)

    def emit(self, now, event, detail):
        if self.closed:
            return
        self.serial += 1
        self.queue.put_nowait(json.dumps(dict(
            event_type='audio_event', turn=0, ts=time.time(),
            occurred_at=now, order_seq=self.serial, agent_id=None,
            detail=dict(event=event, call_id=self.call_id, producer='voice_gateway',
                        audio_seq=self.serial, **detail),
        )))

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.queue.put_nowait(None)
        try:
            await asyncio.wait_for(self.task, timeout=5)
        except asyncio.TimeoutError:
            logger.error('Audio event writer did not drain before teardown: %s', self.call_id)
        except Exception:
            logger.exception('Audio event writer failed: %s', self.call_id)
