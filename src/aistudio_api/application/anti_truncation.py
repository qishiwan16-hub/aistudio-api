"""抗截断模式：通过合成工具调用绕过截断限制，确保完整回答。

使用方式：在模型名称前添加 `抗截断-` 前缀，例如 `抗截断-gemini-2.5-pro`。
通过注入随机命名的合成工具让模型将最终回答放入 functionCall 的 content 参数中，
网关再从 functionCall 中提取内容还原为普通文本，以此绕过外审截断。
流式和非流式均有效。

参考：
- https://github.com/Xeltra233/Antigravity-anti-truncation-gateway
- https://github.com/ken050210/vertex-streaming-anti-truncation
"""

from __future__ import annotations

import asyncio
import io
import json
import os
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

MAX_CONTINUATION_ATTEMPTS = 3

# 心跳间隔（秒）
HEARTBEAT_INTERVAL = 2

# 被视为截断的 finish_reason（需要续传）
_CUT_SHORT_REASONS = frozenset({"MAX_TOKENS", "LENGTH", "RECITATION"})


# ==================== 随机工具名生成 ====================


def _generate_tool_name() -> str:
    """每次请求生成随机合成工具名，降低被上游检测/屏蔽的风险。"""
    return "router_emit_" + os.urandom(12).hex()


def _build_tool_declaration(tool_name: str) -> dict[str, object]:
    """根据工具名构建 Gemini 格式的 function declaration。"""
    return {
        "name": tool_name,
        "description": (
            "Deliver the complete user-visible reply as text. "
            "This function only transports text and performs no external action."
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


def _build_control_instruction(tool_name: str, *, continuation: bool = False) -> str:
    """构建控制指令，告知模型使用合成工具输出回答。"""
    text = (
        "only the new continuation text" if continuation else "the entire user-visible answer"
    )
    base = (
        f"Transport format for this reply: call {tool_name} exactly once. "
        f"Put {text} in its content string, preserving all requested formatting, "
        f"markup, language and sections. The function only transports text and "
        f"performs no external action."
    )
    if continuation:
        base += (
            "\nFollow the existing continuation context and extend the message "
            "being continued from its exact end. Output only the new text to append; "
            "do not repeat, summarize, rewrite or restart the existing text. "
            "Preserve its language, point of view, formatting and any unfinished "
            "sentence or markup. Do not add a preamble, continuation label, "
            "speaker prefix or enclosing quotes unless they belong to the "
            "continuation itself."
        )
    return base


# ==================== 完整性检查 ====================


def _is_cut_short(finish_reason: str | None) -> bool:
    """判断 finish_reason 是否表示输出被截断。"""
    if not finish_reason:
        return False
    return finish_reason.upper() in _CUT_SHORT_REASONS


def _validate_tool_arguments(args_str: str) -> tuple[bool, str]:
    """验证工具调用参数 JSON 是否完整合法。

    Returns:
        (valid, extracted_content)
    """
    if not args_str:
        return False, ""
    try:
        parsed = json.loads(args_str)
        if isinstance(parsed, dict) and "content" in parsed:
            return True, str(parsed["content"])
        return False, ""
    except (json.JSONDecodeError, TypeError):
        # JSON 不完整，尝试正则提取部分内容
        return False, _extract_partial_content(args_str)


def _extract_partial_content(args_str: str) -> str:
    """从不完整的 JSON 字符串中尽力提取 content 字段的部分内容。"""
    match = re.search(
        r'"content"\s*:\s*"((?:[^"\\]|\\.)*)', args_str, re.DOTALL
    )
    if match:
        raw = match.group(1)
        try:
            # 尝试修补并解码
            return json.loads(f'"{raw}"')
        except (json.JSONDecodeError, ValueError):
            return (
                raw.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )
    return ""


def _check_response_integrity(
    output, tool_name: str
) -> dict[str, object]:
    """全面检查响应完整性。

    Returns:
        {
            "valid": bool,              # 响应是否完整
            "has_content": bool,        # 是否有文本内容
            "has_synthetic": bool,      # 是否有合成工具调用
            "has_real_tools": bool,     # 是否有真实工具调用
            "synthetic_content": str,   # 提取的合成工具内容
            "real_calls": list,         # 真实工具调用列表
            "finish_reason": str,       # finish reason
            "needs_continuation": bool, # 是否需要续传
            "reason": str,              # 判断原因
        }
    """
    finish_reason = to_gemini_finish_reason(
        output.candidates[0].finish_reason if output.candidates else None
    )

    result: dict[str, object] = {
        "valid": False,
        "has_content": bool(output.text),
        "has_synthetic": False,
        "has_real_tools": False,
        "synthetic_content": "",
        "real_calls": [],
        "finish_reason": finish_reason,
        "needs_continuation": False,
        "reason": "",
    }

    # 检查 function_calls 中的合成工具和真实工具
    if output.function_calls:
        synthetic_content = ""
        real_calls: list[dict[str, object]] = []

        for fc in output.function_calls:
            name = str(fc.get("name") or "")
            if name == tool_name:
                result["has_synthetic"] = True
                args = fc.get("args") or fc.get("arguments") or {}
                if isinstance(args, dict):
                    content = str(args.get("content", ""))
                    if content:
                        synthetic_content += content
                elif isinstance(args, str):
                    valid_json, content = _validate_tool_arguments(args)
                    if content:
                        synthetic_content += content
                    if not valid_json and not _is_cut_short(finish_reason):
                        result["reason"] = "invalid_tool_arguments"
                        result["synthetic_content"] = synthetic_content
                        result["needs_continuation"] = True
                        return result
                # 也检查 raw 字段
                raw = fc.get("raw")
                if not synthetic_content and isinstance(raw, list) and len(raw) > 1:
                    raw_args = raw[1]
                    if isinstance(raw_args, dict):
                        synthetic_content += str(raw_args.get("content", ""))
            else:
                real_calls.append(fc)

        result["synthetic_content"] = synthetic_content
        result["real_calls"] = real_calls
        result["has_real_tools"] = bool(real_calls)

    # 判断完整性
    has_any_content = bool(
        result["has_content"]
        or result["synthetic_content"]
        or result["has_real_tools"]
    )

    if _is_cut_short(finish_reason):
        # 明确截断
        result["needs_continuation"] = True
        result["reason"] = f"cut_short:{finish_reason}"
    elif finish_reason == "STOP":
        if not has_any_content:
            # STOP 但完全没内容——异常但不续传
            result["reason"] = "empty_completion"
            logger.warning("抗截断完整性检查：STOP 但无内容")
        else:
            result["valid"] = True
            result["reason"] = "complete"
    elif finish_reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"):
        # 安全过滤，不续传
        result["reason"] = f"content_filter:{finish_reason}"
        result["valid"] = not _is_cut_short(finish_reason)
        logger.warning("抗截断完整性检查：安全过滤 (%s)", finish_reason)
    else:
        # 其他 finish_reason (MALFORMED_FUNCTION_CALL 等)
        if has_any_content:
            result["needs_continuation"] = True
            result["reason"] = f"unknown_finish:{finish_reason}"
        else:
            result["reason"] = f"empty_unknown:{finish_reason}"

    return result


# ==================== 请求注入 ====================


def inject_anti_truncation(
    req: GeminiGenerateContentRequest,
    tool_name: str,
    *,
    continuation: bool = False,
) -> GeminiGenerateContentRequest:
    """对请求注入合成工具和控制指令。返回修改后的请求（不修改原始对象）。"""
    import copy

    modified = copy.deepcopy(req)

    # 1. 注入合成工具到 tools 列表
    if modified.tools is None:
        modified.tools = []
    # 清除之前可能残留的旧合成工具（续传场景）
    for tool in modified.tools:
        if tool.functionDeclarations:
            tool.functionDeclarations = [
                decl
                for decl in tool.functionDeclarations
                if not str(decl.get("name", "")).startswith("router_emit_")
            ]
    modified.tools.append(
        GeminiTool(functionDeclarations=[_build_tool_declaration(tool_name)])
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
    control_text = _build_control_instruction(tool_name, continuation=continuation)
    if modified.systemInstruction is None:
        modified.systemInstruction = GeminiContent(
            role="user", parts=[GeminiPart(text=control_text)]
        )
    else:
        # 清除之前的控制指令（续传场景）
        modified.systemInstruction.parts = [
            p
            for p in modified.systemInstruction.parts
            if not (p.text and "router_emit_" in p.text)
        ]
        modified.systemInstruction.parts.append(GeminiPart(text=control_text))

    return modified


def _build_continuation_request(
    original_req: GeminiGenerateContentRequest,
    accumulated_text: str,
    tool_name: str,
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
                f"\n\n你已经输出了约 {len(accumulated_text)} 个字符的内容，"
                f'结尾是：\n"...{accumulated_text[-100:]}"'
            )
        else:
            content_summary = f'\n\n你已经输出的内容是：\n"{accumulated_text}"'

    continuation_prompt = (
        f"你之前的回复被截断了。请调用 `{tool_name}` 工具继续输出剩余的所有内容。"
        f"不要重复前面已经输出的内容，直接继续输出。{content_summary}"
    )
    continued.contents.append(
        GeminiContent(role="user", parts=[GeminiPart(text=continuation_prompt)])
    )

    return continued


# ==================== 响应提取 ====================


def extract_synthetic_content(
    function_calls: list[dict[str, object]],
    tool_name: str,
) -> tuple[str, list[dict[str, object]]]:
    """从 function_calls 中分离合成工具内容和真实工具调用。

    Returns:
        (synthetic_content, real_function_calls)
    """
    synthetic_content = ""
    real_calls: list[dict[str, object]] = []

    for fc in function_calls:
        name = str(fc.get("name") or "")
        if name == tool_name:
            args = fc.get("args") or fc.get("arguments") or {}
            if isinstance(args, dict):
                synthetic_content += str(args.get("content", ""))
            elif isinstance(args, str):
                _, content = _validate_tool_arguments(args)
                if content:
                    synthetic_content += content
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
    tool_name = _generate_tool_name()
    injected_req = inject_anti_truncation(req, tool_name)
    collected_text = ""
    last_output = None
    last_normalized = None

    for continuation in range(MAX_CONTINUATION_ATTEMPTS):
        if continuation == 0:
            current_req = injected_req
        else:
            # 续传时生成新的随机工具名
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
                    "抗截断非流式请求: model=%s, tool=%s, 续传轮次=%d, 重试=%d",
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

            raise HTTPException(500, detail={"message": "抗截断：所有重试均失败"})

        # 完整性检查
        integrity = _check_response_integrity(last_output, tool_name)
        logger.info(
            "抗截断完整性: valid=%s, reason=%s, synthetic=%d字符, text=%d字符",
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

        # 判断是否需要续传
        if not integrity["needs_continuation"]:
            # 完成或无法续传
            final_text = collected_text or last_output.text or ""
            parts = to_gemini_parts(
                final_text,
                function_calls=integrity["real_calls"] or None,
                thinking=last_output.thinking,
                images=last_output.images,
                reasoning_images=last_output.reasoning_images,
            )
            return GeminiGenerateContentResponse(
                candidates=[
                    GeminiCandidateResponse(
                        content=GeminiContentResponse(parts=parts),
                        finishReason=str(integrity["finish_reason"]),
                        index=0,
                    )
                ],
                usageMetadata=to_gemini_usage_metadata(last_output.usage),
                modelVersion=last_normalized.model
                if last_normalized
                else model_path,
                responseId=last_output.response_id or None,
            )

        # 需要续传
        logger.info(
            "抗截断：需要续传 (reason=%s, 已收集=%d字符)",
            integrity["reason"],
            len(collected_text),
        )
        continue

    # 所有续传尝试用尽，返回已收集的内容
    logger.warning(
        "抗截断：达到最大续传次数 (%d)，返回已收集内容 (%d字符)",
        MAX_CONTINUATION_ATTEMPTS,
        len(collected_text),
    )
    parts = (
        to_gemini_parts(collected_text)
        if collected_text
        else [GeminiPartResponse(text="")]
    )
    return GeminiGenerateContentResponse(
        candidates=[
            GeminiCandidateResponse(
                content=GeminiContentResponse(parts=parts),
                finishReason="STOP",
                index=0,
            )
        ],
        usageMetadata=to_gemini_usage_metadata(
            last_output.usage if last_output else None
        ),
        modelVersion=model_path,
    )


# ==================== 流式抗截断 ====================


def build_anti_truncation_stream_response(
    client: AIStudioClient,
    req: GeminiGenerateContentRequest,
    model_path: str,
) -> StreamingResponse:
    """流式抗截断：上游走流式通道收数据，期间发心跳保活，收完后一次性推完整结果。"""

    async def _stream() -> AsyncGenerator[str, None]:
        # 结果容器
        result_chunks: list[str] = []
        work_error: list[Exception] = []
        done_event = asyncio.Event()
        started_event = asyncio.Event()  # Hook拦截成功后触发

        HEARTBEAT_DATA = 'data: {"candidates": [{"content": {"parts": [{"text": ""}], "role": "model"}, "index": 0}]}\n\n'

        async def _do_work():
            """后台执行全部工作：准备请求、调上游、收数据、续传。"""
            try:
                tool_name = _generate_tool_name()
                injected_req = inject_anti_truncation(req, tool_name)
                collected_text = io.StringIO()

                last_finish_reason: str | None = None
                latest_response_id: str | None = None
                latest_usage: dict[str, object] | None = None
                last_normalized = None
                last_thinking_parts: list[str] = []
                last_images: list = []
                last_reasoning_images: list = []
                last_real_calls: list[dict[str, object]] = []

                for continuation in range(MAX_CONTINUATION_ATTEMPTS):
                    if continuation == 0:
                        current_req = injected_req
                    else:
                        tool_name = _generate_tool_name()
                        current_req = _build_continuation_request(
                            injected_req, collected_text.getvalue(), tool_name
                        )
                        current_req = inject_anti_truncation(
                            current_req, tool_name, continuation=True
                        )

                    stream_complete = False

                    for attempt in range(MAX_RETRIES):
                        await ensure_active_account(attempt, model=model_path)
                        try:
                            normalized = normalize_gemini_request(current_req, model_path)
                            logger.info(
                                "抗截断请求: model=%s, tool=%s, 续传轮次=%d, 重试=%d",
                                normalized.model,
                                tool_name,
                                continuation + 1,
                                attempt + 1,
                            )

                            round_text_parts: list[str] = []
                            round_thinking_parts: list[str] = []
                            round_function_calls: list[dict[str, object]] = []
                            round_images: list = []
                            round_reasoning_images: list = []
                            round_finish_reason: int | None = None
                            round_usage: dict[str, object] | None = None
                            round_response_id: str | None = None

                            first_event = True
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
                                # Hook拦截成功，进入流式循环，通知外层开始心跳
                                if first_event:
                                    first_event = False
                                    started_event.set()

                                if event_type == "body" and payload:
                                    round_text_parts.append(str(payload))
                                elif event_type == "thinking" and payload:
                                    round_thinking_parts.append(str(payload))
                                elif event_type == "tool_calls" and payload:
                                    if isinstance(payload, list):
                                        round_function_calls.extend(payload)
                                    else:
                                        round_function_calls.append(payload)
                                elif event_type == "images" and payload:
                                    if isinstance(payload, list):
                                        round_images.extend(payload)
                                    else:
                                        round_images.append(payload)
                                elif event_type == "reasoning_images" and payload:
                                    if isinstance(payload, list):
                                        round_reasoning_images.extend(payload)
                                    else:
                                        round_reasoning_images.append(payload)
                                elif event_type == "usage":
                                    round_usage = payload if isinstance(payload, dict) else None
                                elif event_type == "finish_reason":
                                    round_finish_reason = payload if isinstance(payload, int) else None
                                elif event_type == "response_id":
                                    round_response_id = str(payload)

                            # 如果没收到任何事件也要确保 started
                            started_event.set()

                            record_rotator_event("success", model=normalized.model)
                            runtime_state.record(normalized.model, "success", round_usage)
                            last_normalized = normalized
                            latest_usage = round_usage
                            latest_response_id = round_response_id

                            if continuation == 0:
                                last_thinking_parts = round_thinking_parts
                                last_images = round_images
                                last_reasoning_images = round_reasoning_images

                            synthetic_content, real_calls = extract_synthetic_content(
                                round_function_calls, tool_name
                            )
                            if synthetic_content:
                                collected_text.write(synthetic_content)
                            elif round_text_parts:
                                collected_text.write("".join(round_text_parts))
                            last_real_calls = real_calls

                            last_finish_reason = to_gemini_finish_reason(round_finish_reason)
                            stream_complete = True
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

                    if not stream_complete:
                        logger.error("抗截断：所有重试均失败")
                        result_chunks.append(format_sse_error(
                            HTTPException(
                                500,
                                detail={"message": "抗截断：所有重试均失败"},
                            )
                        ))
                        collected_text.close()
                        return

                    is_cut = _is_cut_short(last_finish_reason)
                    has_content = bool(collected_text.getvalue())

                    if not is_cut:
                        logger.info(
                            "抗截断完成: finish=%s, 已收集=%d字符",
                            last_finish_reason,
                            len(collected_text.getvalue()),
                        )
                        break

                    if not has_content:
                        logger.warning(
                            "抗截断：无内容且截断 (finish=%s)", last_finish_reason
                        )
                        break

                    logger.info(
                        "抗截断：需要续传 (finish=%s, 已收集=%d字符)",
                        last_finish_reason,
                        len(collected_text.getvalue()),
                    )
                    continue

                # 拼接完成，构建结果
                full_text = collected_text.getvalue()
                collected_text.close()
                target_model = last_normalized.model if last_normalized else model_path
                full_thinking = "".join(last_thinking_parts)

                logger.info(
                    "抗截断生成完成: model=%s, 总内容=%d字符",
                    target_model,
                    len(full_text),
                )

                if full_thinking:
                    chunk = format_sse_event(
                        "thinking", full_thinking,
                        model_version=target_model, response_id=latest_response_id,
                    )
                    if chunk:
                        result_chunks.append(chunk)

                if last_reasoning_images:
                    chunk = format_sse_event(
                        "reasoning_images", last_reasoning_images,
                        model_version=target_model, response_id=latest_response_id,
                    )
                    if chunk:
                        result_chunks.append(chunk)

                if full_text:
                    chunk = format_sse_event(
                        "body", full_text,
                        model_version=target_model, response_id=latest_response_id,
                    )
                    if chunk:
                        result_chunks.append(chunk)

                if last_real_calls:
                    chunk = format_sse_event(
                        "tool_calls", last_real_calls,
                        model_version=target_model, response_id=latest_response_id,
                    )
                    if chunk:
                        result_chunks.append(chunk)

                if last_images:
                    chunk = format_sse_event(
                        "images", last_images,
                        model_version=target_model, response_id=latest_response_id,
                    )
                    if chunk:
                        result_chunks.append(chunk)

                result_chunks.append(format_sse_usage(
                    latest_usage,
                    finish_reason="STOP",
                    model_version=target_model,
                    response_id=latest_response_id,
                ))

            except Exception as exc:
                work_error.append(exc)
            finally:
                done_event.set()

        # 立刻启动后台工作
        work_task = asyncio.create_task(_do_work())

        # 等 Hook 拦截成功或工作完成（哪个先到）
        while not started_event.is_set() and not done_event.is_set():
            await asyncio.sleep(0.05)

        # Hook 成功，立刻发第一个心跳
        if not done_event.is_set():
            yield HEARTBEAT_DATA

        # 持续发心跳直到工作完成
        while not done_event.is_set():
            try:
                await asyncio.wait_for(done_event.wait(), timeout=HEARTBEAT_INTERVAL)
            except asyncio.TimeoutError:
                yield HEARTBEAT_DATA

        await work_task

        # 工作完成，推送结果
        if work_error:
            yield format_sse_error(work_error[0])
        else:
            for chunk in result_chunks:
                yield chunk

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
