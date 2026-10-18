"""假流式模式：上游走流式通道收数据（更稳定、不易超时），拼完后以非流式 JSON 返回。

使用方式：在模型名称前添加 `假流式-` 前缀，例如 `假流式-gemini-2.5-pro`。
用于上游非流式接口容易超时或不稳定，希望借助流式通道拿数据、但客户端只需要完整结果的场景。

流程：
1. 对客户端返回 SSE 流式响应（保持连接不断）
2. 调上游流式接口，逐块收数据
3. 收数据期间每隔2秒发 SSE 空数据心跳，防止连接超时
4. 拼接完成后，把完整结果作为一个 SSE data 事件推给客户端
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator

from fastapi.responses import StreamingResponse

from aistudio_api.api.response_models import (
    GeminiCandidateResponse,
    GeminiContentResponse,
    GeminiGenerateContentResponse,
)
from aistudio_api.api.responses import to_gemini_parts, to_gemini_usage_metadata
from aistudio_api.api.schemas import GeminiGenerateContentRequest
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_orchestrator import (
    MAX_RETRIES,
    ensure_active_account,
    record_rotator_event,
)
from aistudio_api.application.api_service_gemini import (
    format_sse_error,
    handle_attempt_exception,
    to_gemini_finish_reason,
)
from aistudio_api.application.chat_service import normalize_gemini_request
from aistudio_api.domain.models import GeneratedImage
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger

from fastapi import HTTPException
import json

logger = get_logger("fake_stream")

# 心跳间隔（秒）
HEARTBEAT_INTERVAL = 2


def build_fake_stream_response(
    client: AIStudioClient,
    req: GeminiGenerateContentRequest,
    model_path: str,
) -> StreamingResponse:
    """假流式：上游走流式通道收数据，期间发心跳保活，收完后一次性推完整结果。"""

    async def _generate() -> AsyncGenerator[str, None]:
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            await ensure_active_account(attempt, model=model_path)
            normalized = None
            try:
                normalized = normalize_gemini_request(req, model_path)
                logger.info(
                    "假流式请求: model=%s, 对话轮数=%s, 尝试第 %d 次",
                    normalized.model,
                    len(req.contents),
                    attempt + 1,
                )

                # ========== 用流式通道收数据，拼接完整结果 ==========
                text_parts: list[str] = []
                thinking_parts: list[str] = []
                all_function_calls: list[dict[str, object]] = []
                all_images: list[GeneratedImage] = []
                all_reasoning_images: list[GeneratedImage] = []
                final_usage: dict[str, object] | None = None
                final_finish_reason: int | None = None
                response_id: str | None = None

                # 用 asyncio.Queue 让心跳协程和数据收集协程并发
                done_event = asyncio.Event()
                collect_error: list[Exception] = []

                async def _collect_upstream():
                    """从上游流式通道收集全部数据。"""
                    nonlocal final_usage, final_finish_reason, response_id
                    try:
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
                            force_refresh_capture=attempt > 0,
                        ):
                            if event_type == "body" and payload:
                                text_parts.append(str(payload))
                            elif event_type == "thinking" and payload:
                                thinking_parts.append(str(payload))
                            elif event_type == "tool_calls" and payload:
                                if isinstance(payload, list):
                                    all_function_calls.extend(payload)
                                else:
                                    all_function_calls.append(payload)
                            elif event_type == "images" and payload:
                                if isinstance(payload, list):
                                    all_images.extend(payload)
                                else:
                                    all_images.append(payload)
                            elif event_type == "reasoning_images" and payload:
                                if isinstance(payload, list):
                                    all_reasoning_images.extend(payload)
                                else:
                                    all_reasoning_images.append(payload)
                            elif event_type == "usage":
                                final_usage = payload if isinstance(payload, dict) else None
                            elif event_type == "finish_reason":
                                final_finish_reason = (
                                    payload if isinstance(payload, int) else None
                                )
                            elif event_type == "response_id":
                                response_id = str(payload)
                    except Exception as e:
                        collect_error.append(e)
                    finally:
                        done_event.set()

                # 启动上游数据收集任务
                collect_task = asyncio.create_task(_collect_upstream())

                # 发送心跳直到数据收集完成
                while not done_event.is_set():
                    try:
                        await asyncio.wait_for(
                            done_event.wait(), timeout=HEARTBEAT_INTERVAL
                        )
                    except asyncio.TimeoutError:
                        # 还没收完，发一个 SSE 心跳（注释行，不影响数据解析）
                        yield "data: {\"candidates\": [{\"content\": {\"parts\": [{\"text\": \"\"}], \"role\": \"model\"}, \"index\": 0}]}\n\n"

                # 确保任务完成
                await collect_task

                # 如果收集过程中出错，抛出异常走重试逻辑
                if collect_error:
                    raise collect_error[0]

                # ========== 拼接完成，构建完整非流式 JSON 作为 SSE data 推出 ==========
                full_text = "".join(text_parts)
                full_thinking = "".join(thinking_parts)

                record_rotator_event("success", model=normalized.model)
                runtime_state.record(normalized.model, "success", final_usage)
                logger.info(
                    "假流式生成完成: model=%s, 输入Tokens=%s, 输出Tokens=%s",
                    normalized.model,
                    final_usage.get("prompt_tokens") if final_usage else 0,
                    final_usage.get("completion_tokens") if final_usage else 0,
                )

                response_obj = GeminiGenerateContentResponse(
                    candidates=[
                        GeminiCandidateResponse(
                            content=GeminiContentResponse(
                                parts=to_gemini_parts(
                                    full_text,
                                    function_calls=all_function_calls,
                                    thinking=full_thinking,
                                    images=all_images,
                                    reasoning_images=all_reasoning_images,
                                ),
                            ),
                            finishReason=to_gemini_finish_reason(final_finish_reason),
                            index=0,
                        )
                    ],
                    usageMetadata=to_gemini_usage_metadata(final_usage),
                    modelVersion=normalized.model,
                    responseId=response_id,
                )

                # 以 SSE data 事件发送完整 JSON 结果
                payload_json = response_obj.model_dump_json(exclude_none=True)
                yield f"data: {payload_json}\n\n"
                return

            except Exception as exc:
                last_error = exc
                if await handle_attempt_exception(
                    exc,
                    attempt=attempt,
                    model_path=model_path,
                    normalized_model=normalized.model if normalized else None,
                    client=client,
                ):
                    continue
                yield format_sse_error(exc)
                return

        # 所有重试都失败
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
