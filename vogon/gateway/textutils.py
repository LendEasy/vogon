"""Text normalisation for comparing what was said (the gateway's own copy of the
application's helper; vogon step 2, slice 3). Pure functions, stdlib only."""
from __future__ import annotations

import re

# Curly / smart quotes + apostrophes (and backtick) -> straight ASCII.
_SMART_SINGLE = "‘’‚‛′`"  # ' ' ‚ ‛ ′ `
_SMART_DOUBLE = "“”„‟″"        # " " „ ‟ ″
_QUOTE_TABLE = {ord(c): "'" for c in _SMART_SINGLE}
_QUOTE_TABLE.update({ord(c): '"' for c in _SMART_DOUBLE})

# A comma sitting between two digits is a thousands separator -> delete it.
# Repeated application via re.sub handles chains like "1,200,000".
_THOUSANDS = re.compile(r"(?<=\d),(?=\d)")

# Runs of any whitespace -> single space.
_WS = re.compile(r"\s+")

# Trailing terminal punctuation at the very end of the string.
_TRAILING_TERMINAL = re.compile(r"[.!?]+$")


def normalize(text: str) -> str:
    """Normalize text for substring comparison. Pure: no side effects."""
    if text is None:
        return ""
    # 1. lowercase
    s = text.lower()
    # 2. smart quotes / apostrophes -> straight ASCII
    s = s.translate(_QUOTE_TABLE)
    # 3. remove digit-grouping commas only (commas between words survive)
    s = _THOUSANDS.sub("", s)
    # 4. collapse whitespace runs to single space, then strip
    s = _WS.sub(" ", s).strip()
    # 5. drop trailing terminal punctuation (.!? at the very end).
    #    Decimal points inside numbers like "202.36" are untouched because the
    #    pattern only anchors to the end of the string.
    s = _TRAILING_TERMINAL.sub("", s).strip()
    return s


def contains(transcript: str, needle: str) -> bool:
    """True if the normalized needle is a substring of the normalized transcript."""
    return normalize(needle) in normalize(transcript)
