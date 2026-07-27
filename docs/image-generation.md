# 图片生成

MiniUnicorn 通过 `generate_image` 工具支持文生图与参考图编辑。在 WebUI 中开启 **Image Generation** 后，对话中即可调用，并支持基于上一张生成图的迭代编辑。

该功能默认关闭。在 `~/.miniunicorn/config.json` 中启用并指定一个已配置的 `model_preset`，重启网关后生效。

## 快速上手

```json
{
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "preset": "my_openai"
    }
  }
}
```

其中 `my_openai` 是 `agents.model_presets` 字典里已配置的预设名（与 Plan & Execute 的 `plannerModel` 同模式）。凭证（API Key / API Base / extra_headers / extra_body）完全复用该 preset，无需在 `imageGeneration` 下重复声明。

> [!TIP]
> 推荐 API Key 通过环境变量注入。在 `model_presets.<name>.apiKey` 中写 `${VAR_NAME}`，MiniUnicorn 启动时从环境解析。

## WebUI 使用

1. 在 Settings → Image 页面打开 **Enable generate_image**，并选择一个模型预设。
2. 在对话 composer 中直接描述要生成的图或要做的编辑。
3. 编辑时把上一轮的 artifact 路径作为 `reference_images` 传回（agent 会自动处理）。

生成的图保存在本地，agent 拿到 artifact 路径后可通过 `message` 工具的 `media` 参数发给用户。

## 配置参考

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `tools.imageGeneration.enabled` | boolean | `false` | 是否注册 `generate_image` 工具 |
| `tools.imageGeneration.preset` | string | `"default"` | 引用的 `model_preset` 名；`"default"` 或空串表示用主模型预设 |
| `tools.imageGeneration.apiType` | string | `"images_generations"` | 协议适配器（运行时由 preset 的 provider 自动推断，配置值会被覆盖） |
| `tools.imageGeneration.responseFormat` | string | `"b64_json"` | 响应格式（运行时由 preset 的 provider 自动推断，配置值会被覆盖） |
| `tools.imageGeneration.defaultAspectRatio` | string | `"1:1"` | 默认宽高比；prompt 或工具调用未指定时使用 |
| `tools.imageGeneration.defaultImageSize` | string | `"1K"` | 默认尺寸提示，如 `1K` / `2K` / `4K` / `1024x1024` |
| `tools.imageGeneration.maxImagesPerTurn` | integer | `4` | 单次工具调用允许的最大张数，范围 1-8 |
| `tools.imageGeneration.saveDir` | string | `"generated"` | 生成图片的保存子目录（相对于 media 根目录） |

`camelCase` 与 `snake_case` 都接受，文档统一用 `camelCase` 以与 `config.json` 对齐。

### apiType 自动推断规则

| preset 的 provider | 推断出的 apiType |
|--------------------|------------------|
| `dashscope` | `dashscope_multimodal`（阿里通义万相 MultiModalConversation） |
| `openrouter` | `chat_completions`（OpenRouter 风格 `/chat/completions` with `modalities=["image"]`） |
| 其他（`openai` / `zhipu` / `stepfun` / `aihubmix` / `minimax` / `gemini` / `ollama` / `custom` / `auto` / 未知 / None） | `images_generations`（OpenAI 标准 `/images/generations`） |

### responseFormat 自动推断规则

仅对 `images_generations` 协议生效；其他协议各自有固定响应结构，不读此字段。

| preset 的 provider | 推断出的 responseFormat |
|--------------------|-------------------------|
| `zhipu` / `stepfun` / `minimax` | `url`（返回临时图片 URL，适配器自动下载转 base64） |
| 其他 | `b64_json`（OpenAI 标准，直接返回 base64） |

## 协议适配器说明

注册表只维护 3 种通用协议适配器（见 [providers/__init__.py](../miniunicorn/agent/tools/image_generation/providers/__init__.py)），不内置任何具体厂商适配器。所有 provider 通过 `apiType` 选择走哪个协议。

### images_generations（OpenAI 标准）

适用：OpenAI `dall-e-3` / `gpt-image-1`、智谱 cogview、阶跃星辰 step-image、AIHubMix、自部署 SD WebUI OpenAI 兼容端点等。

- 请求：`POST {api_base}/images/generations`
- 文生图 Body：`{ "model", "prompt", "n", "size", "response_format", ... }`
- 参考图编辑：当模型属于 `{gpt-image-1, dall-e-2}` 时自动切换到 `POST {api_base}/images/edits` multipart 端点；其他模型退化为文生图 + 在 prompt 中拼接参考图路径（best-effort）。
- 响应：
  - `responseFormat="b64_json"`：`{ "data": [{"b64_json": "..."}] }`
  - `responseFormat="url"`：`{ "data": [{"url": "..."}] }`，适配器下载后转 base64 data URL

### chat_completions（OpenRouter 风格）

适用：OpenRouter 或任何通过 Chat Completions API 返回图片的端点（如 `openai/gpt-image-*` 系列）。

- 请求：`POST {api_base}/chat/completions`
- Body：`{ "model", "messages": [...], "modalities": ["image", "text"], "image_config": {...}, "stream": false }`
- 参考图编辑：通过 `messages.content` 多模态结构以 `image_url` 形式传入。
- 响应：`{ "choices": [{"message": {"images": [{"image_url": {"url": "data:image/..."}}], "content": "..."}}] }`

### dashscope_multimodal（阿里通义万相）

适用：阿里云 DashScope 通义万相系列（`qwen-image-2.0-pro` / `qwen-image-max` / `qwen-image-plus` 等）。

- 请求：`POST {api_base}/services/aigc/multimodal-generation/generation`
- Headers：`Authorization: Bearer {api_key}`（可加 `X-DashScope-Async: enable` 走异步任务模式）
- Body：`{ "model", "input": {"messages": [{"role":"user","content":[{"text":"..."},{"image":"data:image/..."}]}]}, "parameters": {"size":"1024*1024","n":1,"watermark":false,"prompt_extend":true} }`
- 参考图编辑：通过 `messages.content` 的 `image` 字段以 data URL 或 HTTP URL 形式传入。
- 响应：`{ "output": {"choices": [{"message": {"content": [{"image": "https://..."}]}}]} }`，图片 URL 24h 有效，适配器自动下载转 base64。

## Provider 配置示例

所有 provider 共用同一套 `model_presets` 配置机制。在 `agents.model_presets.<name>` 下声明凭证，再在 `tools.imageGeneration.preset` 里引用该名字即可。

### OpenAI（gpt-image-1 / dall-e-3）

OpenAI 官方端点通过 `custom` provider 配置（OpenAI 已不再作为内置 provider 字段存在）。

```json
{
  "agents": {
    "model_presets": {
      "my_openai": {
        "provider": "custom",
        "model": "gpt-image-1",
        "apiKey": "${OPENAI_API_KEY}",
        "apiBase": "https://api.openai.com/v1"
      }
    }
  },
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "preset": "my_openai",
      "defaultAspectRatio": "1:1"
    }
  }
}
```

`gpt-image-1` 支持 `/images/edits` 端点的参考图编辑；`dall-e-3` 仅文生图。

### OpenRouter

```json
{
  "agents": {
    "model_presets": {
      "my_or": {
        "provider": "openrouter",
        "model": "openai/gpt-image-1",
        "apiKey": "${OPENROUTER_API_KEY}"
      }
    }
  },
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "preset": "my_or"
    }
  }
}
```

### 阿里通义万相（DashScope）

```json
{
  "agents": {
    "model_presets": {
      "my_qwen": {
        "provider": "dashscope",
        "model": "qwen-image-2.0-pro",
        "apiKey": "${DASHSCOPE_API_KEY}"
      }
    }
  },
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "preset": "my_qwen"
    }
  }
}
```

通义万相走 `dashscope_multimodal` 协议；支持参考图编辑；宽高比映射到 `WIDTH*HEIGHT` 形式（如 `1:1` → `1024*1024`，`16:9` → `1280*720`）。

### 智谱 cogview

```json
{
  "agents": {
    "model_presets": {
      "my_zhipu": {
        "provider": "zhipu",
        "model": "glm-image",
        "apiKey": "${ZAI_API_KEY}"
      }
    }
  },
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "preset": "my_zhipu"
    }
  }
}
```

走 `images_generations` 协议；`responseFormat` 自动推断为 `url`（返回临时 URL，30 天有效，适配器自动下载）。其他可选模型：`cogview-4` / `cogview-4-250304` / `cogview-3-flash`。不支持参考图编辑。

### 其他 OpenAI 兼容端点

只要 provider 对应的 API 兼容 OpenAI 标准 `/images/generations`，都可走 `images_generations` 协议。在 `model_presets.<name>` 里配置 `provider` / `apiBase` / `apiKey` 后，把 `tools.imageGeneration.preset` 指向该预设即可。

> [!IMPORTANT]
> 旧版本文档曾提到 `tools.imageGeneration.provider` / `tools.imageGeneration.model` 字段，以及 `gemini` / `ollama` / `aihubmix` 原生适配器，这些已全部废弃。当前实现只保留 3 种通用协议适配器，凭证统一通过 `preset` 复用 `model_presets`。

## 进阶：通过 extra_body 传 provider 专属参数

`model_presets.<name>.extraBody` 中的字段会被合并到请求体。可用于传 `quality` / `style` / `seed` / `negative_prompt` 等模型专属字段。

```json
{
  "agents": {
    "model_presets": {
      "my_openai_hq": {
        "provider": "custom",
        "model": "gpt-image-1",
        "apiKey": "${OPENAI_API_KEY}",
        "apiBase": "https://api.openai.com/v1",
        "extraBody": {
          "quality": "high"
        }
      }
    }
  },
  "tools": {
    "imageGeneration": {
      "enabled": true,
      "preset": "my_openai_hq"
    }
  }
}
```

> [!NOTE]
> 没有专用 UI 控件的高级用户可在 `extraBody` 里直接写 provider 专属字段；适配器会原样透传到请求体。

## 工具参数参考

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `prompt` | string | 是 | 图像生成或编辑的描述 |
| `reference_images` | string[] | 否 | 参考图本地路径列表（用于迭代编辑） |
| `aspect_ratio` | string | 否 | 输出宽高比，未指定时用配置默认值 |
| `image_size` | string | 否 | 输出尺寸提示，未指定时用配置默认值 |
| `count` | integer | 否 | 本次生成张数，范围 1-`maxImagesPerTurn` |

## Artifacts

生成的图片按以下结构落盘：

```text
~/.miniunicorn/media/generated/YYYY-MM-DD/img_<id>.<ext>
~/.miniunicorn/media/generated/YYYY-MM-DD/img_<id>.json
```

JSON sidecar 字段：

| 字段 | 含义 |
|------|------|
| `id` | 短 ID，如 `img_ab12cd34ef56` |
| `path` | 本地图片路径（供后续编辑时作为 `reference_images` 传回） |
| `mime` | 探测出的 MIME 类型 |
| `prompt` | 本次生成使用的 prompt |
| `model` | provider 模型名 |
| `provider` | preset 名 |
| `source_images` | 编辑时使用的参考图路径列表 |
| `created_at` | 创建时间戳 |

不要把 base64 图片直接贴进对话。agent 会保持 artifact 路径内部使用，除非用户明确要求调试细节。

## Prompt 写法建议

好的图像 prompt 通常包含：

- 主体与场景
- 构图、镜头或布局
- 风格、氛围、光照、配色
- 必须出现在图中的文字（用引号）
- 约束条件，如"保持同一角色"或"保留 logo"

示例：

```text
A minimal app icon for MiniUnicorn: friendly robot head, rounded square, soft blue and white palette, clean vector style, no text
```

编辑场景的写法：

```text
Use the reference image. Keep the same robot and composition, change the palette to warm orange, and add a subtle sunrise background.
```

## 故障排查

| 症状 | 排查 |
|------|------|
| `generate_image` 不出现在工具列表 | 确认 `tools.imageGeneration.enabled=true` 并重启网关 |
| 提示 Missing API key | 检查 `tools.imageGeneration.preset` 引用的 `model_presets.<name>.apiKey`；若用 `${VAR_NAME}`，确认环境变量在网关进程可见 |
| `unsupported image generation provider` | 当前实现只有 3 种协议适配器；provider 必须能映射到 `images_generations` / `chat_completions` / `dashscope_multimodal` 之一（详见上方"apiType 自动推断规则"） |
| `preset 'xxx' not found in model_presets` | preset 名必须存在于 `agents.model_presets`，或为 `"default"`（用主模型） |
| 生成超时 | 调小 `defaultImageSize`；通过 `extraBody.quality: "low"` 降低质量换速度；或换更稳定的 provider |
| 参考图被拒 | 参考图必须落在 workspace 或 MiniUnicorn media 目录内，扩展名为 png/jpg/jpeg/webp/gif，且 magic bytes 与扩展名一致 |
