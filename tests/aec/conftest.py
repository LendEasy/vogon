"""Standalone AEC tests: the real SpeexDSP AECProcessor on a known echo path.

No network, no providers. The far end is a deterministic speech-like signal
(no audio is tracked in git), echoed back the way the e2e duplex harness does
it: decoded from μ-law, delayed, scaled, mixed in PCM and re-encoded.
"""
# ruff: noqa: E402 — its imports follow the sys.path setup
import sys
from pathlib import Path

import pytest

# This directory's own helpers; the tree root and the contract are on the path from
# tests/conftest.py.
_here = str(Path(__file__).resolve().parent)
if _here not in sys.path:
    sys.path.insert(0, _here)

pytest.importorskip('speexdsp._speexdsp', reason='the AEC tests need the native SpeexDSP extension')
