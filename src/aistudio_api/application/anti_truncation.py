"""Single-pass synthetic-tool delivery for text-generation responses.

The model writes its complete visible reply into a synthetic function-call
argument. The gateway restores that argument as ordinary response text. This
is intended for upstream filters that inspect prose output, not token limits or
transport failures; it does not issue continuation requests.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiGenerateContentResponse,
    GeminiPartResponse,
)
from aistudio_api.api.responses import to_gemini_parts, to_gemini_usage_metadata
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiPart,
    GeminiTool,
)
from aistudio_api.application.account_orchestrator import (
    ensure_active_account,
    record_rotator_event,
)
from aistudio_api.application.api_service_gemini import (
    classify_gemini_error_payload,
    format_sse_error,
    format_sse_event,
    format_sse_usage,
    to_gemini_finish_reason,
)
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.api.state import runtime_state
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("anti_truncation")
HEARTBEAT_INTERVAL = 2


def _generate_tool_name() -> str:
    return "actual_content_" + os.urandom(8).hex()


def _build_tool_declaration(tool_name: str) -> dict[str, object]:
    return {
        "name": tool_name,
        "description": (
            "Shows the complete story text to the reader in this interactive-fiction "
            "session. Call once with the complete reply in content."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "content": {
                    "type": "STRING",
                    "description": "The complete user-visible reply, verbatim.",
                }
            },
            "required": ["content"],
        },
    }


def _build_control_instruction(tool_name: str) -> str:
    return (
        "A small note about how replies are displayed in this session: this is "
        "interactive fiction, and the reader's client displays the story through "
        f"the function `{tool_name}`. For this reply, put the complete text you "
        "would otherwise write in one call, in order, into its content string. "
        "Preserve all requested wording, formatting, and tags verbatim."
    )


def _tool_mode(tool_config: object) -> str | None:
    if not isinstance(tool_config, dict):
        return None
    function_config = tool_config.get("functionCallingConfig") or tool_config.get(
        "function_calling_config"
    )
    if not isinstance(function_config, dict):
        return None
    mode = function_config.get("mode")
    return str(mode).strip().upper() if mode is not None else None


def inject_anti_truncation(
    req: GeminiGenerateContentRequest,
    tool_name: str,
) -> GeminiGenerateContentRequest:
    """Clone and augment a request with a one-shot synthetic text tool."""
    modified = req.model_copy(deep=True)
    if _tool_mode(modified.toolConfig) in {"NONE", "3"}:
        raise HTTPException(
            400,
            detail={
                "message": "抗截断需要启用函数调用；请求显式设置了 tool_choice=none",
                "type": "bad_request",
            },
        )

    tools = list(modified.tools or [])
    tools.append(
        GeminiTool(functionDeclarations=[_build_tool_declaration(tool_name)])
    )
    modified.tools = tools

    instruction = _build_control_instruction(tool_name)
    if modified.systemInstruction is None:
        modified.systemInstruction = GeminiContent(
            role="user", parts=[GeminiPart(text=instruction)]
        )
    else:
        modified.systemInstruction.parts.append(GeminiPart(text=instruction))

    # Leave caller tool-choice policy intact. The private AI Studio wire protocol
    # has no separately verified toolConfig slot, so do not claim hard-required mode.
    return modified


def _decode_content(args: object) -> str | None:
    if isinstance(args, dict):
        value = args.get("content")
        return value if isinstance(value, str) else None
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return None
        return _decode_content(parsed)
    return None


def extract_synthetic_content(
    function_calls: list[dict[str, object]], tool_name: str
) -> tuple[str, list[dict[str, object]]]:
    """Return synthetic text and preserve unrelated function calls."""
    chunks: list[str] = []
    real_calls: list[dict[str, object]] = []
    for call in function_calls:
        if str(call.get("name") or "") != tool_name:
            real_calls.append(call)
            continue
        content = _decode_content(call.get("args"))
        if content is None:
            content = _decode_content(call.get("arguments"))
        if content is None:
            raw = call.get("raw")
            if isinstance(raw, list) and len(raw) > 1:
                content = _decode_content(raw[1])
        if content is not None:
            chunks.append(content)
    return "".join(chunks), real_calls


def _restored_output(output, tool_name: str):
    """Restore synthetic call contents in-place while preserving other fields."""
    for candidate in output.candidates:
        content, real_calls = extract_synthetic_content(candidate.function_calls, tool_name)
        if not content:
            raise HTTPException(
                502,
                detail={
                    "message": "模型未返回合成工具正文；拒绝回退到可能被截断的普通文本",
                    "type": "upstream_error",
                },
            )
        candidate.text = content
        candidate.function_calls = real_calls
    return output


async def handle_anti_truncation_non_stream(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
) -> GeminiGenerateContentResponse:
    """Generate once and restore the synthetic function argument as plain text."""
    normalized = None
    try:
        tool_name = _generate_tool_name()
        injected = inject_anti_truncation(req, tool_name)
        normalized = normalize_gemini_request(injected, model_path)
        await ensure_active_account(0, model=model_path)
        logger.info(
            "抗截断单轮请求: model=%s, tool=%s",
            normalized.model,
            tool_name,
        )
        output = await client.generate_content(
            model=normalized.model,
            capture_prompt=normalized.capture_prompt,
            capture_images=normalized.capture_images,
            contents=normalized.contents,
            system_instruction_content=normalized.system_instruction,
            tools=normalized.tools,
            tool_config=normalized.tool_config,
            safety_settings=normalized.safety_settings,
            temperature=normalized.temperature,
            top_p=normalized.top_p,
            top_k=normalized.top_k,
            max_tokens=normalized.max_tokens,
            generation_config_overrides=normalized.generation_config_overrides,
            sanitize_plain_text=False,
        )

        record_rotator_event("success", model=normalized.model, usage=output.usage)
        runtime_state.record(normalized.model, "success", output.usage)
        output = _restored_output(output, tool_name)
        if not output.candidates:
            raise HTTPException(
                502,
                detail={"message": "模型未返回候选结果", "type": "upstream_error"},
            )
        candidates = []
        for index, candidate in enumerate(output.candidates):
            parts = to_gemini_parts(
                candidate.text,
                function_calls=candidate.function_calls or None,
                function_responses=candidate.function_responses or None,
                thinking=candidate.thinking,
                images=candidate.images,
                reasoning_images=candidate.reasoning_images,
            )
            if candidate.code_output:
                parts.append(GeminiPartResponse(text=candidate.code_output))
            candidates.append(
                GeminiCandidateResponse(
                    content=GeminiContentResponse(parts=parts),
                    finishReason=to_gemini_finish_reason(candidate.finish_reason),
                    index=index,
                )
            )
        return GeminiGenerateContentResponse(
            candidates=candidates,
            usageMetadata=to_gemini_usage_metadata(output.usage),
            modelVersion=normalized.model,
            responseId=output.response_id or None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        code, message, _status = classify_gemini_error_payload(exc)
        raise HTTPException(
            code,
            detail={
                "message": message,
                "type": "upstream_error",
            },
        ) from exc


def build_anti_truncation_stream_response(
    client: AIStudioClient,
    req: GeminiGenerateContentRequest,
    model_path: str,
) -> StreamingResponse:
    """Collect one upstream stream, send heartbeats, then emit the restored reply."""

    async def _stream() -> AsyncGenerator[str, None]:
        result_chunks: list[str] = []
        work_error: list[Exception] = []
        done_event = asyncio.Event()
        heartbeat_data = (
            'data: {"candidates":[{"content":{"parts":[{"text":""}],'
            '"role":"model"},"index":0}]}\n\n'
        )

        async def _do_work() -> None:
            try:
                tool_name = _generate_tool_name()
                injected = inject_anti_truncation(req, tool_name)
                normalized = normalize_gemini_request(injected, model_path)
                text_parts: list[str] = []
                thinking_parts: list[str] = []
                calls: list[dict[str, object]] = []
                images = []
                reasoning_images = []
                usage: dict[str, object] | None = None
                finish_reason: int | None = None
                response_id: str | None = None
                async for event_type, payload in client.stream_generate_content(
                    model=normalized.model,
                    capture_prompt=normalized.capture_prompt,
                    capture_images=normalized.capture_images,
                    contents=normalized.contents,
                    system_instruction_content=normalized.system_instruction,
                    tools=normalized.tools,
                    tool_config=normalized.tool_config,
                    safety_settings=normalized.safety_settings,
                    temperature=normalized.temperature,
                    top_p=normalized.top_p,
                    top_k=normalized.top_k,
                    max_tokens=normalized.max_tokens,
                    generation_config_overrides=normalized.generation_config_overrides,
                    sanitize_plain_text=False,
                ):
                    if event_type == "body" and payload:
                        text_parts.append(str(payload))
                    elif event_type == "thinking" and payload:
                        thinking_parts.append(str(payload))
                    elif event_type == "tool_calls" and payload:
                        calls.extend(payload if isinstance(payload, list) else [payload])
                    elif event_type == "images" and payload:
                        images.extend(payload if isinstance(payload, list) else [payload])
                    elif event_type == "reasoning_images" and payload:
                        reasoning_images.extend(
                            payload if isinstance(payload, list) else [payload]
                        )
                    elif event_type == "usage" and isinstance(payload, dict):
                        usage = payload
                    elif event_type == "finish_reason" and isinstance(payload, int):
                        finish_reason = payload
                    elif event_type == "response_id":
                        response_id = str(payload)
                synthetic_text, _real_calls = extract_synthetic_content(calls, tool_name)
                full_text = synthetic_text
                if not full_text:
                    raise HTTPException(
                        502,
                        detail={
                            "message": "模型未返回合成工具正文",
                            "type": "upstream_error",
                        },
                    )

                record_rotator_event("success", model=normalized.model, usage=usage)
                runtime_state.record(normalized.model, "success", usage)
                logger.info(
                    "抗截断生成完成: model=%s, chars=%d, synthetic_tool=%s",
                    normalized.model,
                    len(full_text),
                    bool(synthetic_text),
                )
                for event, payload in (
                    ("thinking", "".join(thinking_parts)),
                    ("reasoning_images", reasoning_images),
                    ("body", full_text),
                    ("images", images),
                ):
                    if not payload:
                        continue
                    chunk = format_sse_event(
                        event,
                        payload,
                        model_version=normalized.model,
                        response_id=response_id,
                    )
                    if chunk:
                        result_chunks.append(chunk)
                result_chunks.append(
                    format_sse_usage(
                        usage,
                        finish_reason=to_gemini_finish_reason(finish_reason),
                        model_version=normalized.model,
                        response_id=response_id,
                    )
                )
            except Exception as exc:
                work_error.append(exc)
            finally:
                done_event.set()

        work_task = asyncio.create_task(_do_work())
        yield heartbeat_data
        while not done_event.is_set():
            try:
                await asyncio.wait_for(done_event.wait(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                yield heartbeat_data
        await work_task
        if work_error:
            error = work_error[0]
            if isinstance(error, HTTPException):
                detail = error.detail
                message = detail.get("message") if isinstance(detail, dict) else detail
                status = error.status_code
                yield format_sse_error(HTTPException(status, detail=message))
            else:
                yield format_sse_error(error)
        else:
            for chunk in result_chunks:
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
