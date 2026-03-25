from __future__ import annotations

from typing import Any, Dict


_TEXT_FIELDS = ("text", "transcript", "delta")
_NESTED_FIELDS = ("item", "data", "result", "response")

_FINAL_EVENT_TYPES = {
    "conversation.item.input_audio_transcription.completed",
    "response.audio_transcript.done",
}

_PARTIAL_EVENT_TYPES = {
    "conversation.item.input_audio_transcription.text",
    "response.audio_transcript.delta",
}


def _extract_transcription_text(payload: Dict[str, Any]) -> str:
    if not isinstance(payload, dict):
        return ""

    for key in _TEXT_FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value

    for parent in _NESTED_FIELDS:
        child = payload.get(parent)
        if isinstance(child, dict):
            for key in _TEXT_FIELDS:
                value = child.get(key)
                if isinstance(value, str) and value:
                    return value

    return ""


def _is_final_transcription_event(event_type: str) -> bool:
    if event_type in _FINAL_EVENT_TYPES:
        return True
    return event_type.startswith("conversation.item.input_audio_transcription.") and event_type.endswith(".completed")


def _is_partial_transcription_event(event_type: str) -> bool:
    if event_type in _PARTIAL_EVENT_TYPES:
        return True
    return event_type.startswith("conversation.item.input_audio_transcription.") and event_type.endswith(".text")
