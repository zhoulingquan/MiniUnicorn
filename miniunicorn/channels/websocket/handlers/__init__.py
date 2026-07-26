"""WebUI HTTP 路由 handler 集合。

导入本包即触发各子模块的 ``@router.route(...)`` 装饰器执行,完成路由注册。
handler 按功能分组,每个文件负责一组相关端点。

模块清单:
- misc: sessions 列表 / commands / workspaces / sidebar-state
- skills: Skill 增删改查(7 个端点)
- agents: 子代理增删改查 + LLM 生成(5 个端点)
- bootstrap_file: AGENTS.md/SOUL.md 读写 + Dream 文件只读(4 个端点)
- cron: 定时任务管理(4 个端点)
- tools: 用户工具导入/删除(3 个端点)
- channels: 频道配置 + 飞书扫码登录(5 个端点)
- settings: Provider/MCP/搜索等配置(18 个端点,含 async)
- sessions: 会话消息/线程/删除/回退(4 个正则端点)
- media: 签名媒体文件读取(1 个正则端点)
"""

# 导入各 handler 模块,触发装饰器注册。顺序无关(路由表是声明式的)。
from . import (  # noqa: F401
    agents,
    bootstrap_file,
    channels,
    cron,
    media,
    misc,
    sessions,
    skills,
    settings,
    tools,
)
