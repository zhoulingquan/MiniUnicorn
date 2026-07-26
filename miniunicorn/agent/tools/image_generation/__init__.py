"""Image generation tool: text-to-image and image editing through user-defined providers.

工具入口: ImageGenerationTool (name="generate_image")
配置 schema: ImageGenerationConfig (挂在 ToolsConfig.image_generation)
Provider 协议适配: image_generation/providers/

设计原则:
- 三层分离: 工具层 / Provider 适配层 / 持久化层 (复用 utils/artifacts.py)
- Provider 完全用户自定义, 不预置任何具体厂商实现
- 通过 apiType 选择协议适配器 (images_generations / chat_completions / dashscope_multimodal)
- 参考图通过 artifact path 闭环, 形成生成→编辑→再编辑工作流
"""

from miniunicorn.agent.tools.image_generation.tool import ImageGenerationTool

__all__ = ["ImageGenerationTool"]
