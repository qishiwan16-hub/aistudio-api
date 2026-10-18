"""Gemini-compatible API routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from aistudio_api.api.response_models import GeminiGenerateContentResponse
from aistudio_api.application.api_service_gemini import handle_gemini_generate_content
from aistudio_api.infrastructure.gateway.client import AIStudioClient
from aistudio_api.infrastructure.utils.logger import get_logger

from .dependencies import get_client
from .schemas import GeminiGenerateContentRequest

logger = get_logger("routes.gemini")
router = APIRouter()

# 特殊功能变体前缀（长前缀在前，避免短前缀误匹配）
_FAKE_STREAM_ANTI_TRUNCATION_PREFIX = "假流抗截断-"
_FAKE_STREAM_PREFIX = "假流式-"
_ANTI_TRUNCATION_PREFIX = "抗截断-"


def _parse_model_variant(model_path: str) -> tuple[str, str | None]:
    """解析模型路径中的功能变体前缀。

    支持格式（model_path 可能带 models/ 前缀）：
      - models/假流式-gemini-2.5-pro  →  ("models/gemini-2.5-pro", "fake_stream")
      - models/抗截断-gemini-2.5-pro  →  ("models/gemini-2.5-pro", "anti_truncation")
      - 假流式-gemini-2.5-pro         →  ("gemini-2.5-pro", "fake_stream")
      - 抗截断-gemini-2.5-pro         →  ("gemini-2.5-pro", "anti_truncation")
      - models/gemini-2.5-pro         →  ("models/gemini-2.5-pro", None)

    Returns:
        (real_model_path, variant) — variant 为 None 表示普通模式
    """
    # 提取 models/ 前缀
    prefix = ""
    name = model_path
    if name.startswith("models/"):
        prefix = "models/"
        name = name[len("models/"):]

    if name.startswith(_FAKE_STREAM_ANTI_TRUNCATION_PREFIX):
        real_name = name[len(_FAKE_STREAM_ANTI_TRUNCATION_PREFIX):]
        return prefix + real_name, "fake_stream_anti_truncation"
    if name.startswith(_FAKE_STREAM_PREFIX):
        real_name = name[len(_FAKE_STREAM_PREFIX):]
        return prefix + real_name, "fake_stream"
    if name.startswith(_ANTI_TRUNCATION_PREFIX):
        real_name = name[len(_ANTI_TRUNCATION_PREFIX):]
        return prefix + real_name, "anti_truncation"

    return model_path, None


@router.post(
    "/v1beta/{model_path:path}:generateContent",
    response_model=GeminiGenerateContentResponse,
)
async def generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient = Depends(get_client),
):
    real_model, variant = _parse_model_variant(model_path)
    logger.info(
        "收到 Gemini generateContent 请求: model=%s, variant=%s",
        real_model,
        variant or "normal",
    )

    if variant == "fake_stream":
        # 假流式在非流式接口下等同于普通非流式
        return await handle_gemini_generate_content(
            real_model, req, client, stream=False
        )

    if variant == "anti_truncation":
        from aistudio_api.application.anti_truncation import (
            handle_anti_truncation_non_stream,
        )

        return await handle_anti_truncation_non_stream(real_model, req, client)

    if variant == "fake_stream_anti_truncation":
        # 假流抗截断在非流式接口下等同于普通抗截断非流式
        from aistudio_api.application.anti_truncation import (
            handle_anti_truncation_non_stream,
        )

        return await handle_anti_truncation_non_stream(real_model, req, client)

    return await handle_gemini_generate_content(real_model, req, client, stream=False)


@router.post("/v1beta/{model_path:path}:streamGenerateContent")
async def stream_generate_content(
    model_path: str,
    req: GeminiGenerateContentRequest,
    client: AIStudioClient = Depends(get_client),
):
    real_model, variant = _parse_model_variant(model_path)
    logger.info(
        "收到 Gemini streamGenerateContent 流式请求: model=%s, variant=%s",
        real_model,
        variant or "normal",
    )

    if variant == "fake_stream":
        from aistudio_api.application.fake_stream import build_fake_stream_response

        return build_fake_stream_response(client, req, real_model)

    if variant == "anti_truncation":
        from aistudio_api.application.anti_truncation import (
            build_anti_truncation_stream_response,
        )

        return build_anti_truncation_stream_response(client, req, real_model)

    if variant == "fake_stream_anti_truncation":
        from aistudio_api.application.fake_stream_anti_truncation import (
            build_fake_stream_anti_truncation_response,
        )

        return build_fake_stream_anti_truncation_response(client, req, real_model)

    return await handle_gemini_generate_content(real_model, req, client, stream=True)
