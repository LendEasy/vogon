# Contributing

## Developer Certificate of Origin

Contributions are accepted under the [Developer Certificate of Origin 1.1](https://developercertificate.org/).
Sign off every commit (`git commit -s`), which adds a `Signed-off-by:` line
certifying that you have the right to submit the work under the Apache-2.0
license.

## Working on the code

```bash
git clone https://github.com/LendEasy/vogon && cd vogon
python -m venv .venv && . .venv/bin/activate
pip install "setuptools<81" le-vogon-contract && pip install -e ".[front,dev]"
deploy/build_speexdsp.sh .venv/bin/python     # the echo canceller; needs libspeexdsp-dev, swig, g++
ruff check .
python -m pytest tests -q
```

The tests are hermetic: no network, no provider keys, no Redis. A test that
would reach a paid provider call fails at teardown (`tests/conftest.py`). The
live duplex harnesses (a scripted caller through the real audio path against a
real turn service) live with the application that drives them, not here.

## Rules that are enforced by tests

- **The gateway imports nothing from any application.** It knows a turn service
  only through `vogon_contract`.
- **No domain nouns in the gateway.** Fillers, line pools and the front model's
  question come from the turn service's artifacts, never from this code.
- **Timing constants are documented or the test fails.** Change
  `docs/voice-timing-constants.md` in the same commit, and say what moved the
  judgement.
- **Never hide a bug.** No swallowed errors, no weakened assertions, no fallback
  that turns a failure into a silent degraded result.

## Pull requests

One logical change per PR. Say what you measured: for anything that touches
barge-in, the silence clock, delivery or the filler hold, a before/after on the
duplex harness or the relevant offline suite, with the numbers.
