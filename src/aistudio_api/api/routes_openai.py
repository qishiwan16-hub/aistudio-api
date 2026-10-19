"""OpenAI-compatible API routes (/v1/chat/completions, /v1/models).

内置到 aistudio-api，直接在同一端口提供 OpenAI 格式接口。
酒馆 SillyTavern 可直接用 Chat Completion 源连接 http://127.0.0.1:8080/v1
"""

from __future__ import annotations

import contextlib

import json
import time
import uuid
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from aistudio_api.api.dependencies import get_client
from aistudio_api.api.schemas import (
    GeminiContent,
    GeminiGenerateContentRequest,
    GeminiGenerationConfig,
    GeminiInlineData,
    GeminiPart,
)
from aistudio_api.api.state import runtime_state
from aistudio_api.application.api_service_gemini import (
    handle_gemini_generate_content,
    format_sse_event,
    format_sse_usage,
    to_gemini_finish_reason,
)
from aistudio_api.api.routes_gemini import _parse_model_variant
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("routes.openai")
router = APIRouter()


# ==================== 格式转换工具 ====================


def _convert_messages_to_gemini(
    messages: list[dict],
) -> tuple[list[GeminiContent], GeminiContent | None]:
    """OpenAI messages -> Gemini contents + systemInstruction."""
    contents: list[GeminiContent] = []
    system_instruction: GeminiContent | None = None

    for i, msg in enumerate(messages):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # 处理多模态 content (数组格式)
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(GeminiPart(text=part))
                elif isinstance(part, dict):
                    if part.get("type") == "text":
                        parts.append(GeminiPart(text=part.get("text", "")))
                    elif part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            mime, b64 = (
                                url.split(",", 1) if "," in url else ("", url)
                            )
                            mime_type = (
                                mime.split(";")[0].replace("data:", "")
                                if mime
                                else "image/png"
                            )
                            parts.append(
                                GeminiPart(
                                    inlineData=GeminiInlineData(
                                        mimeType=mime_type, data=b64
                                    )
                                )
                            )
                        else:
                            parts.append(GeminiPart(text=f"[image: {url}]"))
            gemini_parts = parts
        else:
            gemini_parts = [GeminiPart(text=str(content))]

        if role == "system":
            # 合并所有 system 消息
            if system_instruction is None:
                system_instruction = GeminiContent(role="user", parts=gemini_parts)
            else:
                system_instruction.parts.extend(gemini_parts)
            continue

        if role == "user":
            gemini_role = "user"
        else:
            # assistant -> model
            gemini_role = "model"

        contents.append(GeminiContent(role=gemini_role, parts=gemini_parts))

    # Gemini 要求第一条必须是 user，最后一条也必须是 user
    if contents and contents[-1].role == "model":
        contents.append(
            GeminiContent(role="user", parts=[GeminiPart(text="继续")])
        )
    if contents and contents[0].role == "model":
        contents.insert(
            0, GeminiContent(role="user", parts=[GeminiPart(text="开始对话")])
        )

    # 确保 user/model 交替（合并连续同 role）
    merged: list[GeminiContent] = []
    for c in contents:
        if merged and merged[-1].role == c.role:
            merged[-1].parts.extend(c.parts)
        else:
            merged.append(c)
    contents = merged

    return contents, system_instruction


def _extract_text_from_gemini(data: dict) -> str:
    """从 Gemini 响应提取文本，跳过 thought。"""
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text_parts = []
        for p in parts:
            if p.get("thought", False):
                continue
            if "text" in p and p["text"]:
                text_parts.append(p["text"])
        return "".join(text_parts)
    except (KeyError, IndexError, TypeError):
        return ""


def _make_openai_chunk(
    chat_id: str, model: str, delta: dict, finish_reason: str | None = None
) -> str:
    """构造 OpenAI SSE 流式 chunk。"""
    chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _make_openai_response(
    chat_id: str, model: str, text: str, usage: dict | None = None
) -> dict:
    """构造 OpenAI 非流式响应。"""
    resp = {
        "id": chat_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }
    if usage:
        resp["usage"] = {
            "prompt_tokens": usage.get("promptTokenCount", usage.get("prompt_tokens", 0)),
            "completion_tokens": usage.get("candidatesTokenCount", usage.get("completion_tokens", 0)),
            "total_tokens": usage.get("totalTokenCount", usage.get("total_tokens", 0)),
        }
    return resp


def _openai_error(status_code: int, message: str, err_type: str = "api_error"):
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": err_type,
                "code": status_code,
            }
        },
    )


# ==================== 路由 ====================


@router.get("/v1/models")
async def openai_list_models():
    """列出可用模型（OpenAI 格式）。"""
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    client = get_client()
    session = client._session if client else None
    models = await model_discovery.get_models(session)

    openai_models = []
    for m in models:
        model_id = m.name.removeprefix("models/")
        openai_models.append(
            {
                "id": model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google",
            }
        )
    return {"object": "list", "data": openai_models}


@router.post("/v1/chat/completions")
async def openai_chat_completions(request: Request):
    """OpenAI Chat Completions 兼容接口。"""
    try:
        body = await request.json()
    except Exception:
        return _openai_error(400, "invalid JSON", "invalid_request_error")

    model = body.get("model", "gemini-2.5-flash")
    stream = body.get("stream", False)
    messages = body.get("messages", [])

    if not messages:
        return _openai_error(400, "messages is required", "invalid_request_error")

    # 转换消息格式
    contents, system_instruction = _convert_messages_to_gemini(messages)

    # 构建生成参数
    gen_config_dict: dict = {}
    if "temperature" in body and body["temperature"] is not None:
        gen_config_dict["temperature"] = body["temperature"]
    if "top_p" in body and body["top_p"] is not None:
        gen_config_dict["topP"] = body["top_p"]
    if "max_tokens" in body and body["max_tokens"] is not None:
        gen_config_dict["maxOutputTokens"] = body["max_tokens"]
    if "top_k" in body and body["top_k"] is not None:
        gen_config_dict["topK"] = body["top_k"]
    if "presence_penalty" in body and body["presence_penalty"] is not None:
        gen_config_dict["presencePenalty"] = body["presence_penalty"]
    if "frequency_penalty" in body and body["frequency_penalty"] is not None:
        gen_config_dict["frequencyPenalty"] = body["frequency_penalty"]

    gen_config = GeminiGenerationConfig(**gen_config_dict) if gen_config_dict else None

    # 构建 Gemini 请求
    gemini_req = GeminiGenerateContentRequest(
        contents=contents,
        systemInstruction=system_instruction,
        generationConfig=gen_config,
    )

    # 解析模型变体前缀
    model_path = f"models/{model}"
    real_model, variant = _parse_model_variant(model_path)

    client = get_client()
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if stream:
        return StreamingResponse(
            _openai_stream(client, gemini_req, real_model, variant, model, chat_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        return await _openai_non_stream(
            client, gemini_req, real_model, variant, model, chat_id
        )


async def _openai_stream(
    client,
    gemini_req: GeminiGenerateContentRequest,
    real_model: str,
    variant: str | None,
    display_model: str,
    chat_id: str,
) -> AsyncGenerator[str, None]:
    """流式：内部调用 Gemini 流式接口，转换成 OpenAI SSE 格式输出。

    在等待上游响应期间每 2 秒发送零宽空格心跳，防止 New API / 酒馆连接超时。
    """
    HEARTBEAT_INTERVAL = 2  # 秒
    HEARTBEAT_CHUNK = _make_openai_chunk(chat_id, display_model, {"content": "\u200b"})

    # 心跳队列：后台 task 往里塞心跳，主循环从里面读
    heartbeat_queue: asyncio.Queue[str | None] = asyncio.Queue()
    got_real_data = asyncio.Event()
    done_event = asyncio.Event()

    async def _heartbeat_sender():
        """后台心跳：收到真正数据前每隔 HEARTBEAT_INTERVAL 发一个零宽空格 chunk。"""
        # 立刻发第一个心跳，确认 SSE 通道畅通
        await heartbeat_queue.put(HEARTBEAT_CHUNK)
        while not done_event.is_set():
            try:
                await asyncio.wait_for(done_event.wait(), timeout=HEARTBEAT_INTERVAL)
                break
            except asyncio.TimeoutError:
                if not got_real_data.is_set():
                    await heartbeat_queue.put(HEARTBEAT_CHUNK)
                # 收到真正数据后停止心跳（由主循环 set got_real_data）
                # 但不退出，等 done_event
        await heartbeat_queue.put(None)  # 哨兵，通知主循环心跳结束

    heartbeat_task = asyncio.create_task(_heartbeat_sender())

    # 先发 role chunk
    yield _make_openai_chunk(chat_id, display_model, {"role": "assistant"})

    try:
        # 根据 variant 选择处理方式，获取 Gemini StreamingResponse
        if variant == "fake_stream":
            from aistudio_api.application.fake_stream import build_fake_stream_response

            gemini_resp = build_fake_stream_response(client, gemini_req, real_model)
        elif variant == "anti_truncation":
            from aistudio_api.application.anti_truncation import (
                build_anti_truncation_stream_response,
            )

            gemini_resp = build_anti_truncation_stream_response(
                client, gemini_req, real_model
            )
        else:
            # 普通流式：直接调 Gemini 流式
            gemini_resp = await handle_gemini_generate_content(
                real_model, gemini_req, client, stream=True
            )

        # 在等待上游期间，先把队列里积攒的心跳 yield 出去
        while not heartbeat_queue.empty():
            hb = heartbeat_queue.get_nowait()
            if hb is not None:
                yield hb

        # gemini_resp 是 StreamingResponse，取其 body_iterator
        async for raw_chunk in gemini_resp.body_iterator:
            # 每次收到数据前，先把心跳队列排空
            while not heartbeat_queue.empty():
                hb = heartbeat_queue.get_nowait()
                if hb is not None:
                    yield hb

            if not raw_chunk or not isinstance(raw_chunk, str):
                continue

            # 解析 SSE data 行
            for line in raw_chunk.strip().split("\n"):
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if not data_str or data_str == "[DONE]":
                    continue

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                text = _extract_text_from_gemini(data)
                if text and text != "\u200b":  # 跳过 Gemini 层心跳零宽空格
                    got_real_data.set()  # 收到真正内容，停止心跳
                    yield _make_openai_chunk(
                        chat_id, display_model, {"content": text}
                    )

    except Exception as e:
        logger.error("OpenAI 流式转换错误: %s", e, exc_info=True)
        yield _make_openai_chunk(
            chat_id, display_model, {"content": f"[Error]: {e}"}, "stop"
        )
        yield "data: [DONE]\n\n"
        return
    finally:
        done_event.set()
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

    # 结束
    yield _make_openai_chunk(chat_id, display_model, {}, "stop")
    yield "data: [DONE]\n\n"


async def _openai_non_stream(
    client,
    gemini_req: GeminiGenerateContentRequest,
    real_model: str,
    variant: str | None,
    display_model: str,
    chat_id: str,
):
    """非流式：内部调用 Gemini 非流式接口，转换成 OpenAI 格式返回。"""
    try:
        if variant == "anti_truncation":
            from aistudio_api.application.anti_truncation import (
                handle_anti_truncation_non_stream,
            )

            gemini_resp = await handle_anti_truncation_non_stream(
                real_model, gemini_req, client
            )
        else:
            gemini_resp = await handle_gemini_generate_content(
                real_model, gemini_req, client, stream=False
            )

        # gemini_resp 是 GeminiGenerateContentResponse
        resp_dict = gemini_resp.model_dump(exclude_none=True)
        text = _extract_text_from_gemini(resp_dict)

        if not text:
            return _openai_error(502, "upstream returned empty response", "upstream_error")

        usage = resp_dict.get("usageMetadata")
        return _make_openai_response(chat_id, display_model, text, usage)

    except Exception as e:
        logger.error("OpenAI 非流式错误: %s", e, exc_info=True)
        return _openai_error(502, str(e), "upstream_error")
