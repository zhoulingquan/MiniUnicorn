"""web_search 独立配置 schema。

不挂在 WebToolsConfig 下,与 web_fetch 平级,便于独立升级维护。

后端选择策略(以 SearXNG 为主力):
- provider="auto" (默认): 并发调用所有已注册后端,合并去重后返回最全面的结果。
  searxng(主力,需配置 base_url) + tavily(AI 摘要,需 Key) + bing_cn(免 Key 兜底)。
  海外后端通过系统代理或 config.proxy 走代理;无代理时自然失败被跳过。
- provider="<name>": 仅调用指定后端,用于调试或特定场景。

结果缓存:默认启用,TTL 固定 1 小时,不暴露给用户配置(默认值已足够,
真有需要可改这里的常量或 config.json)。
"""

from __future__ import annotations

from pydantic import Field

from miniunicorn.config.schema import Base

# 结果缓存内部常量(不暴露到 UI)。如需调整,改这里或 config.json 的同名字段。
DEFAULT_CACHE_TTL_S: int = 3600


class WebSearchBackendConfig(Base):
    """单个后端的独立配置(api_key、base_url 等)。"""

    api_key: str = ""
    base_url: str = ""
    timeout: int = 30


class WebSearchConfig(Base):
    """web_search 工具的独立配置。

    通过根 Config 的 `web_search` 字段访问,与 `tools.web` 平级。
    """

    enable: bool = True
    # auto = 并发调用所有已注册后端,合并去重;指定具体名 = 只调该后端
    provider: str = "auto"
    # 聚合模式(仅 provider=auto 时生效):
    # - "fast" (默认): 首个成功后端返回,其余后台补缓存(低延迟)
    # - "full": 等所有后端返回或超时,全量合并去重(高质量,适合 deep_research)
    # - "hybrid": 首成功返回初步结果,后台继续聚合,下次查询返回增强结果
    aggregate_mode: str = "fast"
    max_results: int = 5
    timeout: int = 30
    # 国外后端专用代理;None 时复用系统环境变量
    proxy: str | None = None
    # 每个后端独立配置。Key 优先级:环境变量 > backends[name].api_key
    # (环境变量优先:凭据不落盘,便于容器/CI 注入并避免明文存 config.json)
    backends: dict[str, WebSearchBackendConfig] = Field(default_factory=dict)

    def get_backend_config(self, name: str) -> WebSearchBackendConfig:
        """取后端配置,不存在则返回默认空配置。"""
        return self.backends.get(name) or WebSearchBackendConfig()

    def get_api_key(self, name: str, env_var: str = "") -> str:
        """取后端 API Key:环境变量优先(凭据不落盘),其次 backends[name].api_key。"""
        if env_var:
            import os

            env_value = os.environ.get(env_var, "").strip()
            if env_value:
                return env_value
        cfg = self.backends.get(name)
        if cfg and cfg.api_key:
            return cfg.api_key
        return ""
