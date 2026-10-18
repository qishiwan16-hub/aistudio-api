"""假流式模式：非流式调用上游等完整结果，拿到后拆成小块模拟 SSE 流式推给前端。

使用方式：在模型名称前添加 `假流式-` 前缀，例如 `假流式-gemini-2.5-pro`。
用于需要流式响应但服务端不支持真流式、或者希望等完整回答再推送的场景。

流程：
1. 调上游非流式接口，等完整响应
2. 拿到完整结果后拆成小块，一股脑快速推给前端
"""

from __future__ import annotations

from typing import AsyncGenerator

from fastapi.responses import StreamingResponse

from aistudio_api.application.api_service_gemini import (
    format_sse_event,
    format_sse_usage,
    to_gemini_finish_reason,
)
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.application.account_orchestrator import (
    MAX_RETRIES,
    ensure_active_account,
    record_rotator_event,
)
from aistudio_api.application.api_service_gemini import handle_attempt_exception
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("fake_stream")

# 每个 SSE chunk 包含的字符数
FAKE_STREAM_CHUNK_SIZE = 50


def build_fake_stream_response(
    client: AIStudioClient,
    req: GeminiGenerateContentRequest,
    model_path: str,
) -> StreamingResponse:
    """构建假流式响应：非流式调用上游等完整结果，拿到后拆块一股脑推给前端。"""

    async def _generate() -> AsyncGenerator[str, None]:
        normalized = None
        for attempt in range(MAX_RETRIES):
            await ensure_active_account(attempt, model=model_path)
            try:
                normalized = normalize_gemini_request(req, model_path)
                logger.info(
                    "假流式请求: model=%s, 对话轮数=%s, 尝试第 %d 次",
                    normalized.model,
                    len(req.contents),
                    attempt + 1,
                )

                # 非流式调用上游，等完整响应
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
                logger.info(
                    "假流式生成完成: model=%s, 输入Tokens=%s, 输出Tokens=%s",
                    normalized.model,
                    output.usage.get("prompt_tokens") if output.usage else 0,
                    output.usage.get("completion_tokens") if output.usage else 0,
                )

                finish_reason = to_gemini_finish_reason(
                    output.candidates[0].finish_reason if output.candidates else None
                )
                response_id = output.response_id or None

                # ========== 拿到完整结果，一股脑拆块推给前端 ==========

                # 1) thinking 内容
                if output.thinking:
                    text = output.thinking
                    for i in range(0, len(text), FAKE_STREAM_CHUNK_SIZE):
                        chunk = format_sse_event(
                            "thinking",
                            text[i : i + FAKE_STREAM_CHUNK_SIZE],
                            model_version=normalized.model,
                            response_id=response_id,
                        )
                        if chunk:
                            yield chunk

                # 2) 推理图片
                if output.reasoning_images:
                    chunk = format_sse_event(
                        "reasoning_images",
                        output.reasoning_images,
                        model_version=normalized.model,
                        response_id=response_id,
                    )
                    if chunk:
                        yield chunk

                # 3) 主要文本内容（分块）
                if output.text:
                    text = output.text
                    for i in range(0, len(text), FAKE_STREAM_CHUNK_SIZE):
                        chunk = format_sse_event(
                            "body",
                            text[i : i + FAKE_STREAM_CHUNK_SIZE],
                            model_version=normalized.model,
                            response_id=response_id,
                        )
                        if chunk:
                            yield chunk

                # 4) 工具调用
                if output.function_calls:
                    chunk = format_sse_event(
                        "tool_calls",
                        output.function_calls,
                        model_version=normalized.model,
                        response_id=response_id,
                    )
                    if chunk:
                        yield chunk

                # 5) 图片
                if output.images:
                    chunk = format_sse_event(
                        "images",
                        output.images,
                        model_version=normalized.model,
                        response_id=response_id,
                    )
                    if chunk:
                        yield chunk

                # 6) usage + finishReason 收尾
                yield format_sse_usage(
                    output.usage,
                    finish_reason=finish_reason,
                    model_version=normalized.model,
                    response_id=response_id,
                )
                return

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

        # 所有重试都失败
        from aistudio_api.application.api_service_gemini import format_sse_error
        from fastapi import HTTPException

        yield format_sse_error(
            HTTPException(
                429,
                detail={
                    "message": f"All accounts reached daily quota for model '{model_path}'.",
                    "type": "rate_limit_exceeded",
                },
            )
        )

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
