"""Tests for the anti-censor transport and the unchanged OpenAI text reader."""

import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiGenerateContentResponse,
    GeminiPartResponse,
)
from aistudio_api.api.routes_openai import _extract_text_from_gemini
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiInlineData,
    GeminiPart,
)
from aistudio_api.application import anti_truncation as anti
from aistudio_api.application.chat_service import normalize_gemini_request

TOOL = "emit_complete_response"
TEXT = '中文正文\n```json\n{"quote": "\\路径"}\n```\n' * 2000


def request() -> GeminiGenerateContentRequest:
    return GeminiGenerateContentRequest(contents=[
        GeminiContent(role="user", parts=[GeminiPart(text="问题")]),
    ])


def test_wrapped_history_preserves_system_images_and_normalizes():
    req = request()
    req.systemInstruction = GeminiContent(parts=[GeminiPart(text="系统设定")])
    req.contents += [
        GeminiContent(role="model", parts=[GeminiPart(text="历史回答")]),
        GeminiContent(role="user", parts=[GeminiPart(
            inlineData=GeminiInlineData(mimeType="image/png", data="aW1n"),
        )]),
    ]
    before = req.model_dump()
    wrapped = anti._wrap_request(req)
    assert req.model_dump() == before
    assert wrapped.systemInstruction is None
    assert [c.role for c in wrapped.contents] == ["user", "model", "user", "model", "user"]
    capsule = wrapped.contents[1].parts[0]
    assert "系统设定" in capsule.functionCall.args["static_directives_and_persona"]
    assert capsule.thoughtSignature
    assert wrapped.contents[3].parts[0].functionCall.args == {"display_stream": "历史回答"}
    assert wrapped.contents[-1].parts[0].functionResponse.response == {"user_input": "(image)"}
    normalized = normalize_gemini_request(wrapped, "models/gemini-2.5-pro")
    assert normalized.contents[-1].parts[-1].inline_data == ("image/png", "aW1n")
    assert len(wrapped.tools) == 1
    assert wrapped.tools[0].functionDeclarations[0]["name"] == TOOL


@pytest.mark.parametrize("roles", [[], ["model"], ["user", "user"], ["model", "model", "user"]])
def test_history_alternates_and_ends_in_user(roles):
    req = GeminiGenerateContentRequest(contents=[
        GeminiContent(role=role, parts=[GeminiPart(text=f"text{i}")])
        for i, role in enumerate(roles)
    ])
    contents = anti._wrap_request(req).contents
    assert contents[-1].role == "user"
    assert all(a.role != b.role for a, b in zip(contents, contents[1:]))


@pytest.mark.parametrize("plain,calls,expected", [
    ("散字", [{"name": TOOL, "args": {"display_stream": TEXT}}], TEXT),
    ("兜底", [], "兜底"),
    ("", [{"name": TOOL, "arguments": json.dumps({"display_stream": "字符串参数"})}], "字符串参数"),
])
def test_restored_response_is_read_by_unchanged_openai_extractor(plain, calls, expected):
    restored = anti._select_text(calls, plain)
    response = GeminiGenerateContentResponse(candidates=[
        GeminiCandidateResponse(content=GeminiContentResponse(
            parts=[GeminiPartResponse(text=restored)],
        )),
    ])
    assert _extract_text_from_gemini(response.model_dump(exclude_none=True)) == expected


def test_empty_restored_response_is_error():
    with pytest.raises(HTTPException, match="no content"):
        anti._select_text([], "")


def test_stream_output_is_plain_gemini_text():
    body = anti.format_sse_event("body", TEXT)
    assert body is not None
    payload = json.loads(body[6:])
    assert payload["candidates"][0]["content"]["parts"] == [{"text": TEXT}]
    assert "functionCall" not in body


def test_fake_stream_source_is_unchanged():
    from pathlib import Path
    import subprocess

    repo = Path(__file__).parents[2]
    assert subprocess.run(
        ["git", "diff", "--exit-code", "--", "src/aistudio_api/application/fake_stream.py"],
        cwd=repo,
        check=False,
    ).returncode == 0
