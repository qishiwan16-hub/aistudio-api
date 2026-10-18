"""Models and metadata routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException

from aistudio_api.api.dependencies import get_runtime_state
from aistudio_api.api.response_models import (
    GeminiModelListResponse,
    GeminiModelResponse,
)
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("routes.models")

if TYPE_CHECKING:
    from aistudio_api.api.state import RuntimeState
router = APIRouter()


def _to_gemini_model(model_data: Mapping[str, object]) -> GeminiModelResponse:
    raw_id = str(model_data.get("id") or model_data.get("name") or "")
    clean_id = raw_id.removeprefix("models/")
    name = f"models/{clean_id}"
    version = str(model_data.get("version") or "001")
    display_name = str(model_data.get("displayName") or clean_id)
    description = str(model_data.get("description") or f"Google {display_name} model")
    input_tokens = int(str(model_data.get("inputTokenLimit") or 1048576))
    output_tokens = int(str(model_data.get("outputTokenLimit") or 8192))
    raw_methods = model_data.get("supportedGenerationMethods")
    methods = (
        [str(x) for x in raw_methods]
        if isinstance(raw_methods, list) and raw_methods
        else [
            "generateContent",
            "countTokens",
            "createCachedContent",
        ]
    )
    raw_temp = model_data.get("temperature")
    temperature = (
        float(raw_temp)
        if isinstance(raw_temp, (int, float)) and not isinstance(raw_temp, bool)
        else 1.0
    )
    raw_top_p = model_data.get("topP")
    top_p = (
        float(raw_top_p)
        if isinstance(raw_top_p, (int, float)) and not isinstance(raw_top_p, bool)
        else 0.95
    )
    raw_top_k = model_data.get("topK")
    top_k = (
        int(raw_top_k)
        if isinstance(raw_top_k, (int, float)) and not isinstance(raw_top_k, bool)
        else 64
    )
    return GeminiModelResponse(
        name=name,
        version=version,
        displayName=display_name,
        description=description,
        inputTokenLimit=input_tokens,
        outputTokenLimit=output_tokens,
        supportedGenerationMethods=methods,
        temperature=temperature,
        topP=top_p,
        topK=top_k,
    )


@router.get("/v1beta/models", response_model=GeminiModelListResponse)
async def list_models(
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> GeminiModelListResponse:
    session = runtime_state.client._session if runtime_state.client else None
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    discovered = await model_discovery.get_models(session=session)
    models = [_to_gemini_model(item) for item in discovered]

    # 为每个模型生成假流式和抗截断变体
    variant_models: list[GeminiModelResponse] = []
    for model in models:
        clean_id = model.name.removeprefix("models/")
        for variant_prefix, desc_suffix in (
            ("假流式-", " (假流式模式：非流式调用后分块模拟 SSE 流式输出)"),
            ("抗截断-", " (抗截断模式：通过合成工具调用绕过截断限制)"),
        ):
            variant_models.append(
                GeminiModelResponse(
                    name=f"models/{variant_prefix}{clean_id}",
                    version=model.version,
                    displayName=f"{variant_prefix}{model.displayName or clean_id}",
                    description=(model.description or "") + desc_suffix,
                    inputTokenLimit=model.inputTokenLimit,
                    outputTokenLimit=model.outputTokenLimit,
                    supportedGenerationMethods=model.supportedGenerationMethods,
                    temperature=model.temperature,
                    topP=model.topP,
                    topK=model.topK,
                )
            )
    models.extend(variant_models)

    logger.info("获取模型列表完成 (共发现 %d 个，含变体)", len(models))
    return GeminiModelListResponse(models=models)


@router.get("/v1beta/models/{model_id:path}", response_model=GeminiModelResponse)
async def get_model(
    model_id: str,
    runtime_state: RuntimeState = Depends(get_runtime_state),
) -> GeminiModelResponse:
    session = runtime_state.client._session if runtime_state.client else None
    from aistudio_api.infrastructure.gateway.model_discovery import model_discovery

    # 解析变体前缀
    _VARIANT_PREFIXES = {"假流式-": " (假流式模式)", "抗截断-": " (抗截断模式)"}
    clean_id = model_id
    variant_suffix = ""
    variant_prefix_str = ""
    for vp, vs in _VARIANT_PREFIXES.items():
        if clean_id.startswith(vp):
            variant_prefix_str = vp
            variant_suffix = vs
            clean_id = clean_id[len(vp):]
            break

    model_info = await model_discovery.get_model_by_id(clean_id, session=session)
    if model_info is not None:
        base = _to_gemini_model(model_info)
        if variant_prefix_str:
            base_clean = base.name.removeprefix("models/")
            base.name = f"models/{variant_prefix_str}{base_clean}"
            base.displayName = f"{variant_prefix_str}{base.displayName or base_clean}"
            base.description = (base.description or "") + variant_suffix
        logger.info("成功获取模型元数据: %s", model_id)
        return base
    raise HTTPException(status_code=404, detail="Model not found")
