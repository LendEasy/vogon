"""LIVE smoke: real Jev read -> real pick ladder -> the words the borrower hears.

    PYTHONPATH=.:../voice-gateway .venv/bin/python tests/smoke_front_complement.py

Needs OPEN_ROUTER_API_KEY. ~25 decisions calls, about $0.001.
"""
import asyncio
import time

from vogon.gateway.settings import settings
from vogon.gateway.transport import front_moves
from vogon.gateway.transport.front_pick import pick
from vogon.gateway.transport.front_source import FrontConfig

PRIME_V = ("Thank you, you're verified. Here's where the account stands: your monthly "
           "payment is $187.36, and the past-due amount today is $202.36.")
PRIME_ID = ("Before I can share any account details I need to verify your identity. "
            "Could you give me your date of birth, the ZIP code on file, or the last "
            "four of your Social Security number?")
PRIME_OPEN = "Hi, this is Maya calling from LendEasy Finance on a recorded line. May I speak with Alex Morgan?"

CASES = [
    (PRIME_V,  "Can I just skip this month's payment and move it to the end?", "expect LOOKUP"),
    (PRIME_V,  "What's the APR on this loan?",                                  "expect LOOKUP"),
    (PRIME_V,  "Can you waive the late fee?",                                   "REFUSAL likely -> BEAT"),
    (PRIME_V,  "Yes, I authorize it.",                                          "READBACK/DONE -> BEAT"),
    (PRIME_V,  "Yes, that amount, date and card are correct.",                  "READBACK -> BEAT"),
    (PRIME_ID, "ZIP? It's 90210.",                                              "identity -> SILENT"),
    (PRIME_ID, "The last four are four four two one.",                          "identity -> SILENT"),
    (PRIME_V,  "I lost my job three weeks ago and I'm behind on everything.",   "distress -> SILENT"),
    (PRIME_V,  "My mother passed away last month.",                             "distress -> SILENT"),
    (PRIME_OPEN, "There's no one here by that name.",                           "stranger -> COURTESY"),
    (PRIME_OPEN, "My wife handles this account for me.",                        "stranger -> COURTESY"),
    (PRIME_V,  "[garbled]",                                                     "unintelligible -> SILENT"),
    (PRIME_V,  "That's everything, thanks, goodbye.",                           "closing -> SILENT"),
    (PRIME_V,  "Sorry, you cut out — I didn't hear that last part.",            "REPEAT"),
    (PRIME_V,  "Can you say that again?",                                       "REPEAT"),
    (PRIME_V,  "Hello? Are you there? I can't hear you.",                       "REPEAT"),
    (PRIME_V,  "How much do I owe again?",                                      "a NEW question -> not a repeat"),
    (PRIME_V,  "I'm not paying anything today.",                                "REFUSAL likely -> BEAT"),
    (PRIME_V,  "Hold on, let me grab my card.",                                 "ROOM is gone -> BEAT/LOOKUP"),
    (PRIME_V,  "I can pay seventy five today and the rest next week.",          "OFFER/ASSENT -> LOOKUP"),
]


async def main():
    path = settings.resolved_front_config_path()
    assert path, "set FRONT_CONFIG_PATH to a full front artifact (the collections one, not examples/front.yaml)"
    cfg = FrontConfig.load(path)
    assert cfg and cfg.can_read_moves, "front artifact did not load"
    used = {}
    def rot(key, pool):
        i = used.get(key, 0)
        used[key] = i + 1
        return pool[i % len(pool)]

    print(f"engine={settings.front_engine} floor={settings.front_move_floor} "
          f"repeat={settings.front_repeat_enabled}\n")
    print(f"{'caller says':54s} {'desk move':11s} {'conf':>5s}  {'tier':8s} what the borrower hears")
    print("-" * 150)
    lat, cost = [], 0.0
    for prime, utt, _note in CASES:
        t = time.perf_counter()
        read = await front_moves.read_async(cfg.move_criteria, cfg.move_riders,
                                            agent_last=prime, utterance=utt)
        ms = (time.perf_counter() - t) * 1000
        lat.append(ms)
        cost += read.cost_usd
        got = pick(read, cfg.tiers, rot,
                   move_floor=settings.front_move_floor)
        on = [k for k, v in read.riders.items() if v >= 0.5]
        print(f"{utt[:52]!r:54s} {read.move:11s} {read.confidence:5.2f}  {got.tier:8s} "
              f"{got.text[:60]!r}")
        if on:
            print(f"{'':54s} {'riders: ' + ', '.join(on):11s}")
    lat.sort()
    print(f"\n{len(CASES)} turns | p50 {lat[len(lat)//2]:.0f} ms | "
          f"p95 {lat[int(0.95*(len(lat)-1))]:.0f} ms | ${cost:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
