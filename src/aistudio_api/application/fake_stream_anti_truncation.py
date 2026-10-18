"""假流抗截断模式：抗截断（合成工具注入+自动续传）+ 假流式（拆块 SSE 推送）。

使用方式：在模型名称前添加 `假流抗截断-` 前缀，例如 `假流抗截断-gemini-2.5-pro`。

流程：
1. 注入随机合成工具 → 非流式调上游
2. 完整性检查 → 如果截断就续传（最多 3 轮）
3. 拿到完整内容后拆块模拟 SSE 流式推给前端
"""

from __future__ import annotations

from typing import AsyncGenerator

from fastapi.responses import StreamingResponse

from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_orchestrator import (
    MAX_RETRIES,
    ensure_active_account,
    record_rotator_event,
)
from aistudio_api.application.anti_truncation import (
    MAX_CONTINUATION_ATTEMPTS,
    _build_continuation_request,
    _check_response_integrity,
    _generate_tool_name,
    inject_anti_truncation,
)
from aistudio_api.application.api_service_gemini import (
    format_sse_error,
    format_sse_event,
    format_sse_usage,
    handle_attempt_exception,
    to_gemini_finish_reason,
)
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("fake_stream_anti_truncation")

# 每个 SSE chunk 包含的字符数
FAKE_STREAM_CHUNK_SIZE = 50


def build_fake_stream_anti_truncation_response(
    client: AIStudioClient,
    req: GeminiGenerateContentRequest,
    model_path: str,
) -> StreamingResponse:
    """假流抗截断：抗截断收集完整内容后，拆块模拟 SSE 流式推送。"""

    async def _generate() -> AsyncGenerator[str, None]:
        tool_name = _generate_tool_name()
        injected_req = inject_anti_truncation(req, tool_name)
        collected_text = ""
        last_output = None
        last_normalized = None

        for continuation in range(MAX_CONTINUATION_ATTEMPTS):
            if continuation == 0:
                current_req = injected_req
            else:
                tool_name = _generate_tool_name()
                current_req = _build_continuation_request(
                    injected_req, collected_text, tool_name
                )
                current_req = inject_anti_truncation(
                    current_req, tool_name, continuation=True
                )

            # 调用上游（带重试）
            for attempt in range(MAX_RETRIES):
                await ensure_active_account(attempt, model=model_path)
                normalized = None
                try:
                    normalized = normalize_gemini_request(current_req, model_path)
                    logger.info(
                        "假流抗截断请求: model=%s, tool=%s, 续传轮次=%d, 重试=%d",
                        normalized.model,
                        tool_name,
                        continuation + 1,
                        attempt + 1,
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
                    record_rotator_event("success", model=normalized.model)
                    runtime_state.record(normalized.model, "success", output.usage)
                    last_output = output
                    last_normalized = normalized
                    break
                except Exception as exc:
                    if await handle_attempt_exception(
                        exc,
                        attempt=attempt,
                        model_path=model_path,
                        normalized_model=normalized.model if normalized else None,
                        client=client,
                    ):
                        continue
                    raise

            if last_output is None:
                from fastapi import HTTPException

                yield format_sse_error(
                    HTTPException(
                        500,
                        detail={"message": "假流抗截断：所有重试均失败"},
                    )
                )
                return

            # 完整性检查
            integrity = _check_response_integrity(last_output, tool_name)
            logger.info(
                "假流抗截断完整性: valid=%s, reason=%s, synthetic=%d字符, text=%d字符",
                integrity["valid"],
                integrity["reason"],
                len(str(integrity["synthetic_content"])),
                len(last_output.text or ""),
            )

            # 收集内容
            if integrity["synthetic_content"]:
                collected_text += str(integrity["synthetic_content"])
            elif last_output.text:
                collected_text += last_output.text

            if not integrity["needs_continuation"]:
                # 完成，跳出续传循环
                break

            logger.info(
                "假流抗截断：需要续传 (reason=%s, 已收集=%d字符)",
                integrity["reason"],
                len(collected_text),
            )
            continue

        # ========== 拿到完整结果，拆块推给前端 ==========
        target_model = last_normalized.model if last_normalized else model_path
        response_id = last_output.response_id if last_output else None

        # 1) thinking
        if last_output and last_output.thinking:
            text = last_output.thinking
            for i in range(0, len(text), FAKE_STREAM_CHUNK_SIZE):
                chunk = format_sse_event(
                    "thinking",
                    text[i : i + FAKE_STREAM_CHUNK_SIZE],
                    model_version=target_model,
                    response_id=response_id,
                )
                if chunk:
                    yield chunk

        # 2) reasoning images
        if last_output and last_output.reasoning_images:
            chunk = format_sse_event(
                "reasoning_images",
                last_output.reasoning_images,
                model_version=target_model,
                response_id=response_id,
            )
            if chunk:
                yield chunk

        # 3) 主要文本内容（分块）
        if collected_text:
            for i in range(0, len(collected_text), FAKE_STREAM_CHUNK_SIZE):
                chunk = format_sse_event(
                    "body",
                    collected_text[i : i + FAKE_STREAM_CHUNK_SIZE],
                    model_version=target_model,
                    response_id=response_id,
                )
                if chunk:
                    yield chunk

        # 4) 真实工具调用
        real_calls = integrity.get("real_calls") if integrity else None
        if real_calls:
            chunk = format_sse_event(
                "tool_calls",
                real_calls,
                model_version=target_model,
                response_id=response_id,
            )
            if chunk:
                yield chunk

        # 5) 图片
        if last_output and last_output.images:
            chunk = format_sse_event(
                "images",
                last_output.images,
                model_version=target_model,
                response_id=response_id,
            )
            if chunk:
                yield chunk

        # 6) usage + finishReason 收尾
        finish_reason = str(integrity["finish_reason"]) if integrity else "STOP"
        if integrity and integrity["needs_continuation"]:
            # 续传用尽但仍截断，仍返回 STOP 让前端正常显示
            finish_reason = "STOP"

        yield format_sse_usage(
            last_output.usage if last_output else None,
            finish_reason=finish_reason,
            model_version=target_model,
            response_id=response_id,
        )

        logger.info(
            "假流抗截断完成: model=%s, 总内容=%d字符",
            target_model,
            len(collected_text),
        )

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
