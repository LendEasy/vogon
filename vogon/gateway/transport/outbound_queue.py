"""Keep a Twilio clear ahead of unsent audio on the gateway's send queue."""
import asyncio
import json


def enqueue_twilio(queue, counter, raw):
    message = json.loads(raw)
    discarded = 0
    if message.get('event') == 'clear':
        retained = []
        while True:
            try:
                entry = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            queue.task_done()
            item = entry[2]
            if item and item[0] == 'text' and json.loads(item[1]).get('event') in ('media', 'mark'):
                discarded += 1
            else:
                retained.append(entry)
        for entry in retained:
            queue.put_nowait(entry)
        priority = -1
    else:
        priority = 0
    queue.put_nowait((priority, next(counter), ('text', raw)))
    return discarded
