"""Hook-first request capture workflow."""

from __future__ import annotations

import asyncio
import json as _json
from dataclasses import dataclass

from aistudio_api.config import DEFAULT_TEXT_MODEL
from aistudio_api.infrastructure.gateway.session import BrowserSession
from aistudio_api.infrastructure.gateway.wire_codec import modify_body
from aistudio_api.infrastructure.gateway.wire_types import AistudioContent
from aistudio_api.infrastructure.utils.logger import get_logger

logger = get_logger("capture")

MIN_BODY_LEN = 4  # 至少需要 MODEL/CONTENTS/SAFETY/GENERATION_CONFIG


def _validate_template_body(raw_body: str) -> bool:
    """验证模板 body 是否为合法的 JSON 数组且长度足够。"""
    try:
        parsed = _json.loads(raw_body)
        return isinstance(parsed, list) and len(parsed) >= MIN_BODY_LEN
    except (ValueError, TypeError):
        return False


@dataclass
class CapturedRequest:
    url: str
    headers: dict[str, str]
    body: str
    model: str = ""
    snapshot: str = ""


class RequestCaptureService:
    """Single-page hook flow modeled after camoufox-api."""

    def __init__(self, session: BrowserSession):
        self._session = session
        self._templates: dict[str, CapturedRequest] = {}
        self._active_ctx_id: int | None = None
        self._lock = asyncio.Lock()

    def clear_templates(self) -> None:
        """清空捕获的请求模板（账号切换或强制刷新时调用）。"""
        self._templates.clear()
        logger.info("已清空捕获模板缓存")

    async def capture(
        self,
        prompt: str,
        model: str = DEFAULT_TEXT_MODEL,
        images: list[str | tuple[str, str]] | None = None,
        contents: list[AistudioContent] | None = None,
        system_instruction: str | None = None,
        system_instruction_content: AistudioContent | None = None,
        tools: list[list] | None = None,
        tool_config: list[object] | None = None,
        safety_settings: list[list] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        max_tokens: int | None = None,
        generation_config_overrides: dict | None = None,
        sanitize_plain_text: bool = True,
        force_refresh: bool = False,
    ) -> CapturedRequest | None:
        if force_refresh:
            self._templates.pop(model, None)
        template = await self._ensure_template(model)
        rewritten_contents = contents
        snapshot_contents = rewritten_contents or [
            AistudioContent.from_user_prompt(prompt=prompt, images=images)
        ]

        # 签名是一次性的票据（根据请求内容穿进 snapshot 计算），每次请求实时生成，不可复用缓存
        snapshot = await self._session.generate_snapshot(snapshot_contents)

        body = modify_body(
            template.body,
            model=model,
            prompt=prompt,
            contents=rewritten_contents,
            system_instruction=system_instruction,
            system_instruction_content=system_instruction_content,
            tools=tools,
            tool_config=tool_config,
            safety_settings=safety_settings,
            images=images,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            max_tokens=max_tokens,
            generation_config_overrides=generation_config_overrides,
            sanitize_plain_text=sanitize_plain_text,
            snapshot=snapshot,
        )
        captured = CapturedRequest(
            url=template.url,
            headers=dict(template.headers),
            body=body,
            model=model,
            snapshot=snapshot,
        )
        logger.info(
            "Hook 拦截成功: model=%s, snapshot=%s chars, body=%s chars",
            captured.model,
            len(captured.snapshot),
            len(captured.body),
        )
        return captured

    async def _ensure_template(self, model: str) -> CapturedRequest:
        ctx = self._session.active_context
        ctx_id = ctx.context_id if ctx else None
        if self._active_ctx_id != ctx_id:
            self._templates.clear()
            self._active_ctx_id = ctx_id

        if model in self._templates:
            return self._templates[model]

        async with self._lock:
            if model in self._templates:
                return self._templates[model]

            captured = await self._session.capture_template(model)
            raw_body = str(captured.get("body") or "")

            if not _validate_template_body(raw_body):
                # base_template 本身就是坏的（bootstrap 时页面没准备好），
                # 重试读同一个缓存没意义，必须强制重做 BotGuard 握手
                logger.warning(
                    "捕获到不完整的模板 body，强制重新执行 BotGuard 握手刷新 base_template"
                )
                try:
                    await self._session.transition_to_account(
                        self._session._auth_file, force_refresh=True
                    )
                except Exception as e:
                    logger.error("BotGuard 重新握手失败: %s", e)
                    raise RuntimeError(
                        "模板 body 不合法且 BotGuard 重新握手失败，无法继续处理请求"
                    ) from e

                # 握手后重新捕获
                captured = await self._session.capture_template(model)
                raw_body = str(captured.get("body") or "")

                if not _validate_template_body(raw_body):
                    logger.error(
                        "BotGuard 重新握手后模板仍不合法 (body=%s)，拒绝缓存",
                        raw_body[:200],
                    )
                    raise RuntimeError(
                        "模板请求体格式不完整，BotGuard 重新握手后仍无法获取合法模板"
                    )

            headers_dict = captured.get("headers")
            headers = {
                str(k): str(v)
                for k, v in (
                    headers_dict.items() if isinstance(headers_dict, dict) else []
                )
            }
            template = CapturedRequest(
                url=str(captured.get("url") or ""),
                headers=headers,
                body=raw_body,
                model=model,
            )
            self._templates[model] = template
            logger.info("Hook 模板已就绪并缓存: model=%s", model)
            return template
