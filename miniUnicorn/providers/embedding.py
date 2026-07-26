"""Dedicated embedding provider.

Supports using a different provider for embeddings than for chat
(e.g., Anthropic Claude for chat + OpenAI for embeddings).
Falls back to the main LLM provider's embed() if no separate
embedding config is provided.
"""
from __future__ import annotations

from typing import Any

from loguru import logger


class EmbeddingProvider:
    """Wraps embedding calls, optionally using a separate config from chat."""

    def __init__(
        self,
        main_provider: Any,
        embedding_config: dict | None = None,
    ):
        self._main = main_provider
        self._embed_client = None
        self._config = embedding_config or {}
        self._separate = bool(self._config.get("embedding_provider"))

    @property
    def enabled(self) -> bool:
        """Whether embeddings are available."""
        if self._separate:
            return True  # Will try on first call
        try:
            # 基类 LLMProvider.embed 默认抛 NotImplementedError,
            # hasattr 会永远返回 True,因此改用方法身份比对:
            # 仅当子类真正 override 了 embed 才视为可用。
            from miniUnicorn.providers.base import LLMProvider

            return type(self._main).embed is not LLMProvider.embed
        except Exception:
            return False

    async def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Generate embeddings, using separate config if available."""
        if not texts:
            return []

        if self._separate:
            return await self._embed_via_separate(texts, model)

        # Fall back to main provider
        try:
            use_model = model or self._config.get("embedding_model", "text-embedding-3-small")
            return await self._main.embed(texts, model=use_model)
        except (NotImplementedError, AttributeError):
            logger.warning("Main provider does not support embeddings; returning empty")
            return []

    async def _embed_via_separate(self, texts: list[str], model: str | None = None) -> list[list[float]]:
        """Use a separate OpenAI-compatible endpoint for embeddings."""
        from openai import AsyncOpenAI

        if self._embed_client is None:
            api_key = self._config.get("embedding_api_key") or ""
            api_base = self._config.get("embedding_api_base") or "https://api.openai.com/v1"
            self._embed_client = AsyncOpenAI(api_key=api_key, base_url=api_base)

        use_model = model or self._config.get("embedding_model", "text-embedding-3-small")
        try:
            resp = await self._embed_client.embeddings.create(model=use_model, input=texts)
            sorted_data = sorted(resp.data, key=lambda x: x.index)
            return [item.embedding for item in sorted_data]
        except Exception:
            logger.exception("Separate embedding failed for model {}", use_model)
            raise

    async def aclose(self) -> None:
        """关闭独立的 embedding 客户端连接池,释放底层 httpx 资源。

        仅在 ``_separate=True`` 时存在独立客户端;主 provider 的连接由其自身
        ``aclose`` 负责。在网关停止或配置重新加载时调用,避免连接泄漏。
        """
        client = self._embed_client
        if client is None:
            return
        # 先置 None,避免并发场景下其他协程在关闭过程中误用。
        self._embed_client = None
        try:
            await client.close()
        except Exception as exc:
            logger.warning("关闭 embedding 客户端连接池失败: {}", exc)
