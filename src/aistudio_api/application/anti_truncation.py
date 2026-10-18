"""抗截断模式：通过合成工具调用绕过截断限制，确保完整回答。

使用方式：在模型名称前添加 `抗截断/` 前缀，例如 `抗截断/gemini-2.5-pro`。
通过注入合成工具 emit_answer 让模型将最终回答放入 functionCall 的 content 参数中，
网关再从 functionCall 中提取内容还原为普通文本，以此绕过外审截断。
流式和非流式均有效。
"""

from __future__ import annotations

import io
import json
import re
from typing import AsyncGenerator

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
    GeminiGenerationConfig,
    GeminiPart,
    GeminiTool,
)
from aistudio_api.api.state import runtime_state
from aistudio_api.application.account_orchestrator import (
    MAX_RETRIES,
    ensure_active_account,
    record_rotator_event,
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

logger = get_logger("anti_truncation")

# ==================== 合成工具配置 ====================

SYNTHETIC_TOOL_NAME = "emit_answer"

SYNTHETIC_TOOL_DECLARATION: dict[str, object] = {
    "name": SYNTHETIC_TOOL_NAME,
    "description": (
        "You MUST call this tool exactly once to output your final user-visible answer. "
        "Put the complete answer in the 'content' argument. "
        "Do NOT output any text outside this tool call."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "content": {
                "type": "STRING",
                "description": "The complete final answer to output to the user.",
            },
        },
        "required": ["content"],
    },
}

CONTROL_INSTRUCTION = f"""严格执行以下输出规则：

1. 你必须调用 `{SYNTHETIC_TOOL_NAME}` 工具来输出你的最终回答
2. 将完整的回答内容放入该工具的 `content` 参数中
3. 不要在普通文本中输出任何内容，所有回答必须通过 `{SYNTHETIC_TOOL_NAME}` 工具输出
4. 如果你的回答被截断，系统会要求你继续输出剩余内容
5. 续传时，将剩余内容继续通过 `{SYNTHETIC_TOOL_NAME}` 工具输出

这个规则对于确保输出完整性极其重要，请严格遵守。"""

CONTINUATION_PROMPT = f"""你之前的回复被截断了。请调用 `{SYNTHETIC_TOOL_NAME}` 工具继续输出剩余的所有内容。

重要提醒：
1. 不要重复前面已经输出的内容
2. 直接继续输出，无需任何前言或解释
3. 将剩余内容放入 `{SYNTHETIC_TOOL_NAME}` 工具的 `content` 参数中

现在请继续输出："""

MAX_CONTINUATION_ATTEMPTS = 3


# ==================== 请求注入 ====================


def inject_anti_truncation(
    req: GeminiGenerateContentRequest,
) -> GeminiGenerateContentRequest:
    """对请求注入合成工具和控制指令。返回修改后的请求（不修改原始对象）。"""
    import copy

    modified = copy.deepcopy(req)

    # 1. 注入合成工具到 tools 列表
    if modified.tools is None:
        modified.tools = []
    already_injected = any(
        decl.get("name") == SYNTHETIC_TOOL_NAME
        for tool in modified.tools
        if tool.functionDeclarations
        for decl in tool.functionDeclarations
    )
    if not already_injected:
        modified.tools.append(
            GeminiTool(functionDeclarations=[SYNTHETIC_TOOL_DECLARATION])
        )

    # 2. 确保 toolConfig 允许工具调用
    if modified.toolConfig is None:
        modified.toolConfig = {}
    fcc = modified.toolConfig.get("functionCallingConfig") or {}
    mode = fcc.get("mode", "")
    if mode in ("NONE", "none", 3):
        fcc["mode"] = "AUTO"
    elif not mode:
        fcc["mode"] = "AUTO"
    modified.toolConfig["functionCallingConfig"] = fcc

    # 3. 注入控制指令到 systemInstruction
    if modified.systemInstruction is None:
        modified.systemInstruction = GeminiContent(
            role="user", parts=[GeminiPart(text=CONTROL_INSTRUCTION)]
        )
    else:
        has_control = any(
            SYNTHETIC_TOOL_NAME in (p.text or "")
            for p in modified.systemInstruction.parts
        )
        if not has_control:
            modified.systemInstruction.parts.append(
                GeminiPart(text=CONTROL_INSTRUCTION)
            )

    return modified


def _build_continuation_request(
    original_req: GeminiGenerateContentRequest,
    accumulated_text: str,
) -> GeminiGenerateContentRequest:
    """构建续传请求：在对话末尾追加已输出内容和续传指令。"""
    import copy

    continued = copy.deepcopy(original_req)

    # 追加模型已输出的内容作为 assistant 历史
    if accumulated_text:
        continued.contents.append(
            GeminiContent(
                role="model", parts=[GeminiPart(text=accumulated_text)]
            )
        )

    # 构建续传指令
    content_summary = ""
    if accumulated_text:
        if len(accumulated_text) > 200:
            content_summary = (
                f"\n\n前面你已经输出了约 {len(accumulated_text)} 个字符的内容，"
                f'结尾是：\n"...{accumulated_text[-100:]}"'
            )
        else:
            content_summary = f'\n\n前面你已经输出的内容是：\n"{accumulated_text}"'

    detailed_prompt = f"{CONTINUATION_PROMPT}{content_summary}"
    continued.contents.append(
        GeminiContent(role="user", parts=[GeminiPart(text=detailed_prompt)])
    )

    return continued


# ==================== 响应提取 ====================


def _extract_content_from_json_str(args_str: str) -> str | None:
    """从 JSON 字符串中提取 content 字段（带正则 fallback）。"""
    try:
        parsed = json.loads(args_str)
        if isinstance(parsed, dict) and "content" in parsed:
            return str(parsed["content"])
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(
        r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"', args_str, re.DOTALL
    )
    if match:
        raw = match.group(1)
        try:
            return json.loads(f'"{raw}"')
        except (json.JSONDecodeError, ValueError):
            return (
                raw.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
    return None


def extract_synthetic_content(
    function_calls: list[dict[str, object]],
) -> tuple[str, list[dict[str, object]]]:
    """从 function_calls 中分离合成工具内容和真实工具调用。

    Returns:
        (synthetic_content, real_function_calls)
    """
    synthetic_content = ""
    real_calls: list[dict[str, object]] = []

    for fc in function_calls:
        name = str(fc.get("name") or "")
        if name == SYNTHETIC_TOOL_NAME:
            args = fc.get("args") or fc.get("arguments") or {}
            if isinstance(args, dict):
                synthetic_content += str(args.get("content", ""))
            elif isinstance(args, str):
                extracted = _extract_content_from_json_str(args)
                if extracted:
                    synthetic_content += extracted
            # 也检查 raw 字段
            raw = fc.get("raw")
            if not synthetic_content and isinstance(raw, list) and len(raw) > 1:
                raw_args = raw[1]
                if isinstance(raw_args, dict):
                    synthetic_content += str(raw_args.get("content", ""))
        else:
            real_calls.append(fc)

    return synthetic_content, real_calls


# ==================== 非流式抗截断 ====================


async def handle_anti_truncation_non_stream(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient,
) -> GeminiGenerateContentResponse:
    """非流式抗截断：多轮续传直到获得完整回答。"""
    injected_req = inject_anti_truncation(req)
    collected_text = ""

    for continuation in range(MAX_CONTINUATION_ATTEMPTS):
        if continuation == 0:
            current_req = injected_req
        else:
            current_req = _build_continuation_request(injected_req, collected_text)
            current_req = inject_anti_truncation(current_req)

        # 调用上游（带重试）
        last_output = None
        for attempt in range(MAX_RETRIES):
            await ensure_active_account(attempt, model=model_path)
            normalized = None
            try:
                normalized = normalize_gemini_request(current_req, model_path)
                logger.info(
                    "抗截断非流式请求: model=%s, 续传轮次=%d, 重试=%d",
                    normalized.model, continuation + 1, attempt + 1,
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
            raise HTTPException(500, detail={"message": "抗截断：所有重试均失败"})

        # 检查是否有合成工具调用
        if last_output.function_calls:
            synthetic, real_calls = extract_synthetic_content(last_output.function_calls)
            if synthetic:
                collected_text += synthetic
                logger.info(
                    "抗截断：收到合成工具内容 (长度=%d, 总长度=%d)",
                    len(synthetic), len(collected_text),
                )

                finish_reason = to_gemini_finish_reason(
                    last_output.candidates[0].finish_reason
                    if last_output.candidates
                    else None
                )

                # 如果 finish_reason 是 STOP，说明完整输出完毕
                if finish_reason == "STOP":
                    # 构建最终响应：合成工具内容 + 真实工具调用
                    parts = to_gemini_parts(
                        collected_text,
                        function_calls=real_calls if real_calls else None,
                        thinking=last_output.thinking,
                        images=last_output.images,
                        reasoning_images=last_output.reasoning_images,
                    )
                    return GeminiGenerateContentResponse(
                        candidates=[
                            GeminiCandidateResponse(
                                content=GeminiContentResponse(parts=parts),
                                finishReason=finish_reason,
                                index=0,
                            )
                        ],
                        usageMetadata=to_gemini_usage_metadata(last_output.usage),
                        modelVersion=normalized.model if normalized else model_path,
                        responseId=last_output.response_id or None,
                    )
                # 未完成，继续下一轮
                continue

        # 模型未使用合成工具，直接输出普通文本
        text = last_output.text or ""
        if text:
            collected_text += text

        finish_reason = to_gemini_finish_reason(
            last_output.candidates[0].finish_reason
            if last_output.candidates
            else None
        )

        # STOP 表示自然结束
        if finish_reason == "STOP":
            parts = to_gemini_parts(
                collected_text or text,
                function_calls=last_output.function_calls or None,
                thinking=last_output.thinking,
                images=last_output.images,
                reasoning_images=last_output.reasoning_images,
            )
            return GeminiGenerateContentResponse(
                candidates=[
                    GeminiCandidateResponse(
                        content=GeminiContentResponse(parts=parts),
                        finishReason=finish_reason,
                        index=0,
                    )
                ],
                usageMetadata=to_gemini_usage_metadata(last_output.usage),
                modelVersion=normalized.model if normalized else model_path,
                responseId=last_output.response_id or None,
            )

        # MAX_TOKENS 等截断，继续续传
        logger.info("抗截断：检测到截断 (finish_reason=%s), 准备续传", finish_reason)
        continue

    # 所有续传尝试用尽，返回已收集的内容
    logger.warning("抗截断：达到最大续传次数 (%d)，返回已收集内容", MAX_CONTINUATION_ATTEMPTS)
    parts = to_gemini_parts(collected_text) if collected_text else [GeminiPartResponse(text="")]
    return GeminiGenerateContentResponse(
        candidates=[
            GeminiCandidateResponse(
                content=GeminiContentResponse(parts=parts),
                finishReason="STOP",
                index=0,
            )
        ],
        usageMetadata=to_gemini_usage_metadata(last_output.usage if last_output else None),
        modelVersion=model_path,
    )


# ==================== 流式抗截断 ====================


def build_anti_truncation_stream_response(
    client: AIStudioClient,
    req: GeminiGenerateContentRequest,
    model_path: str,
) -> StreamingResponse:
    """流式抗截断：实时透传内容，检测截断后自动续传。"""

    async def _stream() -> AsyncGenerator[str, None]:
        injected_req = inject_anti_truncation(req)
        collected_text = io.StringIO()
        forwarded_any = False

        for continuation in range(MAX_CONTINUATION_ATTEMPTS):
            if continuation == 0:
                current_req = injected_req
            else:
                current_req = _build_continuation_request(
                    injected_req, collected_text.getvalue()
                )
                current_req = inject_anti_truncation(current_req)

            found_synthetic = False
            last_finish_reason: str | None = None
            latest_response_id: str | None = None
            latest_usage: dict[str, object] | None = None
            normalized = None
            stream_complete = False

            for attempt in range(MAX_RETRIES):
                await ensure_active_account(attempt, model=model_path)
                has_yielded = False
                try:
                    normalized = normalize_gemini_request(current_req, model_path)
                    logger.info(
                        "抗截断流式请求: model=%s, 续传轮次=%d, 重试=%d",
                        normalized.model, continuation + 1, attempt + 1,
                    )
                    async for event_type, text in client.stream_generate_content(
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
                        if event_type == "usage":
                            latest_usage = text if isinstance(text, dict) else None
                        elif event_type == "finish_reason":
                            last_finish_reason = (
                                to_gemini_finish_reason(text)
                                if isinstance(text, int)
                                else str(text)
                            )
                        elif event_type == "response_id":
                            latest_response_id = str(text)
                        elif event_type == "thought_signature":
                            pass  # 暂不处理
                        elif event_type == "tool_calls":
                            # 检查是否是合成工具
                            fc_list = text if isinstance(text, list) else []
                            synthetic, real_calls = extract_synthetic_content(
                                fc_list
                            )
                            if synthetic:
                                found_synthetic = True
                                collected_text.write(synthetic)
                                # 将合成工具内容作为普通文本 chunk 发送
                                chunk = format_sse_event(
                                    "body",
                                    synthetic,
                                    model_version=normalized.model,
                                    response_id=latest_response_id,
                                )
                                if chunk:
                                    has_yielded = True
                                    forwarded_any = True
                                    yield chunk
                            if real_calls:
                                # 真实工具调用原样透传
                                chunk = format_sse_event(
                                    "tool_calls",
                                    real_calls,
                                    model_version=normalized.model,
                                    response_id=latest_response_id,
                                )
                                if chunk:
                                    has_yielded = True
                                    yield chunk
                        elif event_type == "body":
                            # 普通文本：实时透传
                            if not found_synthetic and text:
                                collected_text.write(str(text))
                                forwarded_any = True
                            chunk = format_sse_event(
                                event_type,
                                text,
                                model_version=normalized.model,
                                response_id=latest_response_id,
                            )
                            if chunk:
                                has_yielded = True
                                yield chunk
                        else:
                            # thinking, images 等直接透传
                            chunk = format_sse_event(
                                event_type,
                                text,
                                model_version=normalized.model,
                                response_id=latest_response_id,
                            )
                            if chunk:
                                has_yielded = True
                                yield chunk

                    stream_complete = True
                    break
                except Exception as exc:
                    if await handle_attempt_exception(
                        exc,
                        attempt=attempt,
                        model_path=model_path,
                        normalized_model=normalized.model if normalized else None,
                        client=client,
                        has_yielded_data=has_yielded,
                    ):
                        continue
                    raise

            if not stream_complete:
                logger.error("抗截断流式：所有重试均失败")
                break

            target_model = normalized.model if normalized else model_path
            record_rotator_event("success", model=target_model)
            if normalized:
                runtime_state.record(normalized.model, "success", latest_usage)

            # 判断是否需要续传
            if found_synthetic and last_finish_reason == "STOP":
                logger.info("抗截断流式：合成工具输出完成")
                break
            if last_finish_reason == "STOP":
                logger.info("抗截断流式：自然结束 (无合成工具)")
                break

            # 截断，准备续传
            logger.info(
                "抗截断流式：检测到截断 (finish_reason=%s, 已收集=%d字符), 续传",
                last_finish_reason,
                len(collected_text.getvalue()),
            )
            continue

        # 发送 usage 收尾
        target_model = normalized.model if normalized else model_path
        yield format_sse_usage(
            latest_usage,
            finish_reason=last_finish_reason or "STOP",
            model_version=target_model,
            response_id=latest_response_id,
        )
        collected_text.close()

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
