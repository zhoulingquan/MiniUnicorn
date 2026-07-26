# Install and Quick Start

## Install

> [!IMPORTANT]
> This README may describe features that are available first in the latest source code.
> If you want the newest features and experiments, install from source.
> If you want the most stable day-to-day experience, install from PyPI or with `uv`.

**Install from source** (latest features, experimental changes may land here first; recommended for development)

```bash
git clone https://github.com/zhoulingquan/miniunicorn.git
cd miniunicorn
pip install -e .
```

**Install with [uv](https://github.com/astral-sh/uv)** (stable release, fast)

```bash
uv tool install miniunicorn-ai
```

**Install from PyPI** (stable release)

```bash
pip install miniunicorn-ai
```

### Update to latest version

**PyPI / pip**

```bash
pip install -U miniunicorn-ai
miniunicorn --version
```

**uv**

```bash
uv tool upgrade miniunicorn-ai
miniunicorn --version
```

## Quick Start

> [!TIP]
> Set your API key in `~/.miniunicorn/config.json`.
> Get API keys: [OpenRouter](https://openrouter.ai/keys) (Global)
>
> For other LLM providers, please see [`configuration.md`](./configuration.md).

**1. Initialize**

```bash
miniunicorn onboard
```

Use `miniunicorn onboard --wizard` if you want the interactive setup wizard.

**2. Configure** (`~/.miniunicorn/config.json`)

Configure these **two parts** in your config (other options have defaults).

*Set your API key* (e.g. OpenRouter, recommended for global users):
```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  }
}
```

*Set your model* (optionally pin a provider — defaults to auto-detection):
```json
{
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5",
      "provider": "openrouter"
    }
  }
}
```

**3. Chat**

```bash
miniunicorn agent
```

That's it! You have a working AI agent in 2 minutes.
