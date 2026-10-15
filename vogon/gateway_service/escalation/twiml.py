"""TwiML generators for the escalation warm-transfer flow.

Every spoken line is either a <Play> of the line rendered in the persona's
voice (escalation/voice.py — ``audio_url``) or, when that render is absent,
a Twilio <Say> of the same text in ``voice``.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

# Twilio <Say> voice for a line that was not rendered in the persona's voice
# — the deployment can pick a closer match with GATEWAY_ESCALATION_SAY_VOICE.
DEFAULT_SAY_VOICE = "Polly.Joanna-Neural"

WHISPER_PROMPT = "Press any key when you are ready to connect the caller."

_XML = '<?xml version="1.0" encoding="UTF-8"?>'


def _speak(parent: ET.Element, text: str, voice: str,
           audio_url: str | None) -> None:
    if audio_url:
        ET.SubElement(parent, "Play").text = audio_url
    else:
        ET.SubElement(parent, "Say", voice=voice).text = text


def hold_twiml(
    hold_message: str,
    conference_name: str,
    hold_music_url: str | None = None,
    voice: str = DEFAULT_SAY_VOICE,
    audio_url: str | None = None,
) -> str:
    """
    TwiML served to the caller when escalation starts.
    1. Speaks the hold_message.
    2. Puts the caller into a named Conference room (they hear hold music there).

    hold_music_url: if None, Twilio plays its default hold music.
    conference_name: must be unique per call (use call_sid as suffix).
    waitUrl: the URL Twilio polls for hold music TwiML while the caller waits.
    """
    resp = ET.Element("Response")
    _speak(resp, hold_message, voice, audio_url)
    dial = ET.SubElement(resp, "Dial")
    conf_attrs = {
        "startConferenceOnEnter": "false",  # don't start until agent joins
        "endConferenceOnExit": "true",      # end conference when caller leaves
        "beep": "false",
        "statusCallbackEvent": "join leave",
    }
    if hold_music_url:
        conf_attrs["waitUrl"] = hold_music_url
    ET.SubElement(dial, "Conference", **conf_attrs).text = conference_name
    return _XML + ET.tostring(resp, encoding="unicode")


def whisper_twiml(summary_text: str, gather_action_url: str,
                  voice: str = DEFAULT_SAY_VOICE,
                  audio_url: str | None = None) -> str:
    """
    TwiML served to the agent when they pick up the outbound dial.
    1. Speaks the summary (and the keypress prompt) to the agent only.
    2. Gathers a single keypress — agent presses any key to connect the caller.
    """
    resp = ET.Element("Response")
    gather = ET.SubElement(resp, "Gather",
        numDigits="1",
        action=gather_action_url,
        method="POST",
        timeout="60",   # wait up to 60s for agent to press key
    )
    _speak(gather, f"{summary_text} {WHISPER_PROMPT}", voice, audio_url)
    # If agent doesn't press within timeout, hang up (no-op — status callback handles retry)
    ET.SubElement(resp, "Hangup")
    return _XML + ET.tostring(resp, encoding="unicode")


def agent_connect_twiml(conference_name: str) -> str:
    """
    TwiML returned when agent presses a key (gather action).
    Puts the agent into the Conference room — this connects them with the caller.
    """
    resp = ET.Element("Response")
    dial = ET.SubElement(resp, "Dial")
    ET.SubElement(dial, "Conference",
        startConferenceOnEnter="true",
        endConferenceOnExit="true",
        beep="false",
    ).text = conference_name
    return _XML + ET.tostring(resp, encoding="unicode")


def voicemail_twiml(voicemail_script: str, voice: str = DEFAULT_SAY_VOICE,
                    audio_url: str | None = None) -> str:
    """TwiML for when we reach an agent's voicemail — reads the script then hangs up."""
    resp = ET.Element("Response")
    _speak(resp, voicemail_script, voice, audio_url)
    ET.SubElement(resp, "Hangup")
    return _XML + ET.tostring(resp, encoding="unicode")


def callback_twiml(caller_callback_message: str, voice: str = DEFAULT_SAY_VOICE,
                   audio_url: str | None = None) -> str:
    """TwiML read to the caller when all agents are unavailable."""
    resp = ET.Element("Response")
    _speak(resp, caller_callback_message, voice, audio_url)
    ET.SubElement(resp, "Hangup")
    return _XML + ET.tostring(resp, encoding="unicode")
