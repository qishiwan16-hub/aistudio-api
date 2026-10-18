# st_bridge.py - aistudio-api -> OpenAI 兼容桥接 (支持流式/非流式)
# 依赖: pip install fastapi uvicorn httpx
# 启动: python st_bridge.py
# 或跟随 aistudio.sh 自动启动
# SillyTavern: Base URL 填 http://127.0.0.1:8081/v1

import httpx, uuid, json, time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AIStudio = "http://127.0.0.1:8080"  # aistudio-api 地址


def openai_error(status_code: int, message: str, err_type: str = "api_error"):
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": err_type, "code": status_code}}
    )


def forward_auth(request: Request) -> dict:
    headers = {}
    for h in ["authorization", "x-goog-api-key", "x-api-key"]:
        if h in request.headers:
            headers[h] = request.headers[h]
    return headers


def convert_messages(messages: list) -> list:
    """OpenAI messages -> Gemini contents"""
    contents = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # 处理多模态 content (数组格式)
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append({"text": part})
                elif isinstance(part, dict):
                    if part.get("type") == "text":
                        parts.append({"text": part.get("text", "")})
                    elif part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:"):
                            # Base64 内联图片
                            mime, b64 = url.split(",", 1) if "," in url else ("", url)
                            mime_type = mime.split(";")[0].replace("data:", "") if mime else "image/png"
                            parts.append({"inlineData": {"mimeType": mime_type, "data": b64}})
                        else:
                            parts.append({"text": f"[image: {url}]"})
            gemini_parts = parts
        else:
            gemini_parts = [{"text": str(content)}]

        if role == "user":
            gemini_role = "user"
        elif role == "system":
            # system 当作 user
            gemini_role = "user"
        else:
            # assistant -> model, 但最后一条不能是 model
            if i == len(messages) - 1:
                gemini_role = "user"
            else:
                gemini_role = "model"

        contents.append({"role": gemini_role, "parts": gemini_parts})
    return contents


def extract_text(data: dict) -> str:
    """从 Gemini 响应提取文本，跳过 thought"""
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


def make_chunk(chat_id: str, model: str, delta: dict, finish_reason=None) -> str:
    """构造 SSE 流式 chunk"""
    chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason
        }]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


@app.get("/v1/models")
async def models(request: Request):
    headers = forward_auth(request)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{AIStudio}/v1beta/models", headers=headers)
    except httpx.RequestError as e:
        return openai_error(502, f"cannot reach aistudio-api: {e}", "connection_error")
    if resp.status_code != 200:
        return openai_error(resp.status_code, resp.text)
    data = resp.json()
    openai_models = []
    for m in data.get("models", []):
        model_id = m["name"].split("/")[-1]
        openai_models.append({
            "id": model_id,
            "object": "model",
            "owned_by": "google"
        })
    return {"object": "list", "data": openai_models}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    try:
        body = await request.json()
    except Exception:
        return openai_error(400, "invalid JSON", "invalid_request_error")

    model = body.get("model", "gemini-2.5-flash")
    stream = body.get("stream", False)
    messages = body.get("messages", [])
    contents = convert_messages(messages)

    # 构建 Gemini payload
    payload = {"contents": contents}

    # 传递生成参数
    gen_config = {}
    if "temperature" in body:
        gen_config["temperature"] = body["temperature"]
    if "top_p" in body:
        gen_config["topP"] = body["top_p"]
    if "max_tokens" in body:
        gen_config["maxOutputTokens"] = body["max_tokens"]
    if gen_config:
        payload["generationConfig"] = gen_config

    auth_headers = forward_auth(request)
    headers = {"Content-Type": "application/json", **auth_headers}
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"

    if stream:
        return StreamingResponse(
            stream_generate(model, payload, headers, chat_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            }
        )
    else:
        return await non_stream_generate(model, payload, headers, chat_id)


async def stream_generate(model: str, payload: dict, headers: dict, chat_id: str):
    """SSE 流式生成"""
    # 先发 role chunk
    yield make_chunk(chat_id, model, {"role": "assistant"}, None)

    url = f"{AIStudio}/v1beta/models/{model}:streamGenerateContent?alt=sse"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0)) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    error_text = ""
                    async for chunk in resp.aiter_text():
                        error_text += chunk
                    yield make_chunk(chat_id, model, {"content": f"[Error {resp.status_code}]: {error_text[:500]}"}, "stop")
                    yield "data: [DONE]\n\n"
                    return

                buffer = ""
                async for raw_chunk in resp.aiter_text():
                    buffer += raw_chunk
                    # SSE 事件以双换行分隔
                    while "\n\n" in buffer:
                        event_str, buffer = buffer.split("\n\n", 1)
                        # 提取 data: 行
                        data_line = ""
                        for line in event_str.split("\n"):
                            if line.startswith("data: "):
                                data_line = line[6:]
                            elif line.startswith("data:"):
                                data_line = line[5:]

                        if not data_line or data_line.strip() == "[DONE]":
                            continue

                        try:
                            data = json.loads(data_line)
                        except json.JSONDecodeError:
                            continue

                        text = extract_text(data)
                        if text:
                            yield make_chunk(chat_id, model, {"content": text}, None)

    except httpx.RequestError as e:
        yield make_chunk(chat_id, model, {"content": f"[Connection Error]: {e}"}, "stop")
        yield "data: [DONE]\n\n"
        return

    # 结束
    yield make_chunk(chat_id, model, {}, "stop")
    yield "data: [DONE]\n\n"


async def non_stream_generate(model: str, payload: dict, headers: dict, chat_id: str):
    """非流式生成"""
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(
                f"{AIStudio}/v1beta/models/{model}:generateContent",
                json=payload,
                headers=headers
            )
    except httpx.RequestError as e:
        return openai_error(502, f"cannot reach aistudio-api: {e}", "connection_error")

    if resp.status_code != 200:
        try:
            upstream = resp.json()
            msg = upstream.get("error", {}).get("message") or resp.text
        except Exception:
            msg = resp.text
        return openai_error(resp.status_code, msg)

    data = resp.json()
    text = extract_text(data)
    if not text:
        return openai_error(502, "aistudio-api returned empty response", "upstream_error")

    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop"
        }],
        "usage": data.get("usageMetadata", {})
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
