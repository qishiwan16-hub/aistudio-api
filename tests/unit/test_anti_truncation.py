from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiPart,
)
from aistudio_api.application.anti_truncation import (
    _build_tool_declaration,
    extract_synthetic_content,
    handle_anti_truncation_non_stream,
    inject_anti_truncation,
)
from aistudio_api.domain.models import Candidate, ModelOutput


def test_tool_declaration_and_request_injection_preserve_existing_tools():
    request = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="write a story")])],
        tools=[{"functionDeclarations": [{"name": "lookup", "parameters": {"type": "OBJECT"}}]}],
    )
    injected = inject_anti_truncation(request, "actual_content_test")

    assert len(injected.tools or []) == 2
    synthetic = injected.tools[-1].functionDeclarations[0]
    assert synthetic == _build_tool_declaration("actual_content_test")
    assert "actual_content_test" in injected.systemInstruction.parts[-1].text
    assert len(request.tools or []) == 1
    assert request.systemInstruction is None


def test_injection_rejects_explicit_tool_choice_none():
    request = GeminiGenerateContentRequest(
        contents=[GeminiContent(role="user", parts=[GeminiPart(text="hello")])],
        toolConfig={"functionCallingConfig": {"mode": "NONE"}},
    )
    with pytest.raises(HTTPException) as caught:
        inject_anti_truncation(request, "actual_content_test")
    assert caught.value.status_code == 400


def test_extract_synthetic_content_removes_only_synthetic_call():
    calls = [
        {"name": "actual_content_test", "args": {"content": "one"}},
        {"name": "actual_content_test", "arguments": '{"content":"two"}'},
        {"name": "weather", "args": {"city": "Tokyo"}},
    ]
    content, remaining = extract_synthetic_content(calls, "actual_content_test")
    assert content == "onetwo"
    assert remaining == [calls[-1]]


@pytest.mark.asyncio
async def test_non_stream_returns_restored_plain_text_and_no_synthetic_tool(monkeypatch):
    from aistudio_api.application import anti_truncation as anti
    from aistudio_api.application.chat_service import NormalizedGeminiRequest

    normalized = NormalizedGeminiRequest(
        model="gemini-4-argon",
        contents=[],
        system_instruction=None,
        tools=[],
        safety_settings=None,
        capture_prompt="hello",
        capture_images=None,
    )
    output = ModelOutput(
        candidates=[
            Candidate(
                text="",
                thinking="reasoning",
                function_calls=[
                    {"name": "actual_content_test", "args": {"content": "full story"}},
                    {"name": "weather", "args": {"city": "Tokyo"}},
                ],
                finish_reason=1,
            )
        ],
        usage={"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    )

    monkeypatch.setattr(anti, "_generate_tool_name", lambda: "actual_content_test")
    monkeypatch.setattr(anti, "normalize_gemini_request", lambda *_: normalized)
    monkeypatch.setattr(anti, "ensure_active_account", _async_noop)
    monkeypatch.setattr(anti, "record_rotator_event", lambda *a, **kw: None)
    monkeypatch.setattr(anti.runtime_state, "record", lambda *a, **kw: None)

    class Client:
        async def generate_content(self, **kwargs):
            assert kwargs["tools"] == normalized.tools
            return output

    response = await handle_anti_truncation_non_stream(
        "gemini-4-argon", GeminiGenerateContentRequest(contents=[]), Client()
    )
    parts = response.candidates[0].content.parts
    assert [part.text for part in parts if part.text] == ["reasoning", "full story"]
    assert [part.functionCall.name for part in parts if part.functionCall] == ["weather"]
    assert response.candidates[0].finishReason == "STOP"


@pytest.mark.asyncio
async def test_non_stream_rejects_missing_synthetic_content(monkeypatch):
    from aistudio_api.application import anti_truncation as anti
    from aistudio_api.application.chat_service import NormalizedGeminiRequest

    normalized = NormalizedGeminiRequest(
        model="gemini-4-argon", contents=[], system_instruction=None, tools=[],
        safety_settings=None, capture_prompt="", capture_images=None,
    )
    monkeypatch.setattr(anti, "_generate_tool_name", lambda: "actual_content_test")
    monkeypatch.setattr(anti, "normalize_gemini_request", lambda *_: normalized)
    monkeypatch.setattr(anti, "ensure_active_account", _async_noop)
    monkeypatch.setattr(anti, "record_rotator_event", lambda *a, **kw: None)
    monkeypatch.setattr(anti.runtime_state, "record", lambda *a, **kw: None)

    class Client:
        async def generate_content(self, **kwargs):
            return ModelOutput(
                candidates=[
                    Candidate(
                        text="possibly truncated",
                        function_calls=[{"name": "actual_content_test", "args": {}}],
                    )
                ]
            )

    with pytest.raises(HTTPException) as caught:
        await handle_anti_truncation_non_stream(
            "gemini-4-argon", GeminiGenerateContentRequest(contents=[]), Client()
        )
    assert caught.value.status_code == 502


async def _async_noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_stream_keeps_heartbeat_until_single_result_is_ready(monkeypatch):
    from aistudio_api.application import anti_truncation as anti
    from aistudio_api.application.chat_service import NormalizedGeminiRequest

    normalized = NormalizedGeminiRequest(
        model="gemini-4-argon", contents=[], system_instruction=None, tools=[],
        safety_settings=None, capture_prompt="", capture_images=None,
    )
    monkeypatch.setattr(anti, "_generate_tool_name", lambda: "actual_content_test")
    monkeypatch.setattr(anti, "normalize_gemini_request", lambda *_: normalized)
    monkeypatch.setattr(anti, "ensure_active_account", _async_noop)
    monkeypatch.setattr(anti, "record_rotator_event", lambda *a, **kw: None)
    monkeypatch.setattr(anti.runtime_state, "record", lambda *a, **kw: None)
    monkeypatch.setattr(anti, "HEARTBEAT_INTERVAL", 0.01)

    class Client:
        async def stream_generate_content(self, **kwargs):
            yield ("status", 200)
            await asyncio.sleep(0.03)
            yield ("tool_calls", [{"name": "actual_content_test", "args": {"content": "complete"}}])
            yield ("finish_reason", 1)
            yield ("usage", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3})

    response = anti.build_anti_truncation_stream_response(
        Client(), GeminiGenerateContentRequest(contents=[]), "gemini-4-argon"
    )
    frames = [frame async for frame in response.body_iterator]
    joined = "".join(frame.decode() if isinstance(frame, bytes) else frame for frame in frames)
    assert '"text":""' in joined
    assert '"text": "complete"' in joined or '"text":"complete"' in joined
    assert "actual_content_test" not in joined
    assert '"finishReason":"STOP"' in joined or '"finishReason": "STOP"' in joined
