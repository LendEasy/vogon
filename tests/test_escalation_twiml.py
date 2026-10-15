"""Unit tests for vogon/gateway_service/escalation/twiml.py.

All five TwiML generator functions are pure — no mocking needed.
We parse the XML output using xml.etree.ElementTree and assert on
element structure, attributes, and text content.

Run with:
    PYTHONPATH=. python3 -m pytest tests/test_escalation_twiml.py -v
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from vogon.gateway_service.escalation.twiml import (
    agent_connect_twiml,
    callback_twiml,
    hold_twiml,
    voicemail_twiml,
    whisper_twiml,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>'


def _parse(xml_str: str) -> ET.Element:
    """Strip the XML declaration and parse into an ElementTree Element."""
    body = xml_str
    if body.startswith("<?xml"):
        body = body[body.index("?>") + 2 :]
    return ET.fromstring(body)


# ---------------------------------------------------------------------------
# hold_twiml
# ---------------------------------------------------------------------------

class TestHoldTwiml:
    def test_hold_twiml_structure(self):
        out = hold_twiml("Please hold.", "conf-abc123")
        assert out.startswith(XML_DECL)
        root = _parse(out)
        assert root.tag == "Response"
        assert root.find("Say") is not None
        assert root.find("Dial") is not None
        conf = root.find(".//Conference")
        assert conf is not None, "Expected <Conference> element"
        assert conf.get("startConferenceOnEnter") == "false"
        assert conf.get("endConferenceOnExit") == "true"
        assert "conf-abc123" in out

    def test_hold_twiml_with_hold_music(self):
        url = "https://example.com/hold.mp3"
        out = hold_twiml("Please hold.", "conf-xyz", hold_music_url=url)
        conf = _parse(out).find(".//Conference")
        assert conf is not None
        assert conf.get("waitUrl") == url

    def test_hold_twiml_no_hold_music(self):
        out = hold_twiml("Please hold.", "conf-xyz", hold_music_url=None)
        conf = _parse(out).find(".//Conference")
        assert conf is not None
        assert conf.get("waitUrl") is None

    def test_hold_twiml_message_text(self):
        msg = "Our team will be with you shortly."
        out = hold_twiml(msg, "conf-123")
        assert msg in out

    def test_hold_twiml_conference_name_in_element(self):
        name = "conf-unique-99"
        out = hold_twiml("Hold please.", name)
        conf = _parse(out).find(".//Conference")
        assert conf is not None
        assert conf.text == name


# ---------------------------------------------------------------------------
# whisper_twiml
# ---------------------------------------------------------------------------

class TestWhisperTwiml:
    def test_whisper_twiml_structure(self):
        out = whisper_twiml("Caller needs help.", "https://example.com/gather")
        assert out.startswith(XML_DECL)
        root = _parse(out)
        assert root.tag == "Response"
        gather = root.find("Gather")
        assert gather is not None
        assert gather.get("numDigits") == "1"
        assert gather.get("action") == "https://example.com/gather"
        assert root.find("Hangup") is not None

    def test_whisper_twiml_includes_summary(self):
        summary = "John Smith is calling about a loan."
        out = whisper_twiml(summary, "https://example.com/gather")
        assert summary in out

    def test_whisper_twiml_includes_key_prompt(self):
        out = whisper_twiml("Summary text.", "https://example.com/gather")
        assert "Press any key" in out

    def test_whisper_twiml_gather_has_say_child(self):
        out = whisper_twiml("Call summary.", "https://example.com/gather")
        root = _parse(out)
        gather = root.find("Gather")
        say = gather.find("Say")
        assert say is not None

    def test_whisper_twiml_action_url_present(self):
        action_url = "https://myapp.com/agent-ready"
        out = whisper_twiml("Summary.", action_url)
        assert action_url in out


# ---------------------------------------------------------------------------
# agent_connect_twiml
# ---------------------------------------------------------------------------

class TestAgentConnectTwiml:
    def test_agent_connect_twiml_structure(self):
        out = agent_connect_twiml("conf-main-001")
        assert out.startswith(XML_DECL)
        root = _parse(out)
        assert root.tag == "Response"
        conf = root.find(".//Conference")
        assert conf is not None
        assert conf.get("startConferenceOnEnter") == "true"
        assert conf.get("endConferenceOnExit") == "true"

    def test_agent_connect_twiml_conference_name(self):
        name = "conf-main-007"
        out = agent_connect_twiml(name)
        conf = _parse(out).find(".//Conference")
        assert conf is not None
        assert conf.text == name
        assert name in out

    def test_agent_connect_twiml_has_dial(self):
        out = agent_connect_twiml("conf-abc")
        root = _parse(out)
        assert root.find("Dial") is not None


# ---------------------------------------------------------------------------
# voicemail_twiml
# ---------------------------------------------------------------------------

class TestVoicemailTwiml:
    def test_voicemail_twiml_structure(self):
        out = voicemail_twiml("Leaving a voicemail for the agent.")
        assert out.startswith(XML_DECL)
        root = _parse(out)
        assert root.tag == "Response"
        assert root.find("Say") is not None
        assert root.find("Hangup") is not None

    def test_voicemail_twiml_script_text(self):
        script = "Hi, this is an automated message from your booking system."
        out = voicemail_twiml(script)
        assert script in out

    def test_voicemail_twiml_say_before_hangup(self):
        out = voicemail_twiml("Please call back.")
        root = _parse(out)
        children = list(root)
        tags = [c.tag for c in children]
        assert tags.index("Say") < tags.index("Hangup")


# ---------------------------------------------------------------------------
# callback_twiml
# ---------------------------------------------------------------------------

class TestCallbackTwiml:
    def test_callback_twiml_structure(self):
        out = callback_twiml("All agents are busy. We will call you back.")
        assert out.startswith(XML_DECL)
        root = _parse(out)
        assert root.tag == "Response"
        assert root.find("Say") is not None
        assert root.find("Hangup") is not None

    def test_callback_twiml_message_text(self):
        msg = "Our agents are unavailable. You will receive a callback."
        out = callback_twiml(msg)
        assert msg in out

    def test_callback_twiml_say_before_hangup(self):
        out = callback_twiml("Goodbye.")
        root = _parse(out)
        children = list(root)
        tags = [c.tag for c in children]
        assert tags.index("Say") < tags.index("Hangup")
