<div align="center">

<img src="docs/logo.svg" alt="Erza Logo" width="200" height="200">

**Open-source, self-hosted Agent Runtime**

Turn any LLM into a long-running, governable, auditable agent system —
a transparent execution kernel, deterministic governance, and a pluggable edge.

[![Python](https://img.shields.io/badge/python-≥3.11-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![Release](https://img.shields.io/badge/release-v0.4.0-success)](https://github.com/zhoulingquan/Erza/releases)
[![Status](https://img.shields.io/badge/status-alpha-orange)]()

[简体中文](./README.md) | **[English]**

</div>

---

## What is this

Erza is not a personal AI assistant, and not a chatbot framework. **Erza is an Agent Runtime**: an infrastructure layer that sits below the LLM and above the application, turning "a model that talks" into "a software system that works, stays up, and can be audited".

Read from the code, it consists of three parts:

- **Execution kernel** (~3.9k lines): one fixed ReAct loop and one turn state machine. No plugin hook chains, no middleware stacks, no dynamic orchestration — read `agent/loop.py` and `agent/runner.py` and you understand every path the agent can take.
- **Governance mechanisms**: a per-turn call ledger, structured tool receipts, evidence-based step acceptance, and a governed memory lifecycle. None of these rely on model good behavior — they are all deterministic code.
- **Edge surfaces**: 5 IM channels + WebSocket, an OpenAI-compatible HTTP API, a Python SDK, and a WebUI console. All traffic enters from the edge and converges into one message bus.

Built on top of [Nanobot](https://github.com/marm-io/nanobot). The project is named after Erza from *Fairy Tail*: a stable core, with equipment swapped in from the armory on demand — mirroring the **core + library** architecture.

> *"If you're not the model, you're the harness."* — once model capability crosses a threshold, agent productivity is decided by the engineering infrastructure wrapping the model. Erza is a complete implementation of such a harness.

### Good fit / poor fit

**Good fit**: building long-running, stateful, auditable agent applications (vertical agents, ops agents, engineering assistants); self-hosted deployments that need IM ingress or an HTTP API; studying the implementation of agent kernels, memory governance, and tool governance.

**Poor fit**: scenarios that need a complex DAG workflow engine; multi-tenant SaaS (the current model is single-workspace isolation); high-sandbox environments that disallow filesystem / shell access.

## Architecture

The system revolves around an async message bus, in four layers:

<div align="center">

<img src="docs/architecture.svg" alt="Erza four-layer architecture: channels → bus → agent kernel → capability layer" width="680">

</div>

```
Edge      channels/(Feishu·WeChat·WeCom·DingTalk·QQ·WebSocket)   api_compat/(OpenAI-compat)   cli/   webui/(console)
             │                    │                        │
             └────────────┬───────┴────────────┬───────────┘
                          ▼                    ▼
                   bus/MessageBus ──────► command/ slash-command router
                          │
                          ▼
Kernel    agent/  AgentLoop (turn state machine) ──► AgentRunner (ReAct loop)
             │              │                    │
             │              │             ┌──────┴──────┐
             │              ▼             ▼             ▼
Governance  │        ledger/CallLedger  planner/  step_acceptance (receipts + evidence)
             │
             ├──► tools/registry ──► tools/* (25+ built-ins) ＋ mcp_runtime (MCP servers)
             ├──► providers/ (multi-provider + fallback chain)
             ├──► memory/ (SQLite structured memory + Dream distillation) ＋ session/ (persistence)
             └──► security/ (workspace boundary · SSRF guard · sandbox · risk levels)

Control   webui/ Python gateway ──► React 18 frontend (settings/channels/tools/memory)
Roots     composition/  gateway / agent / serve assembly
```

Inbound channel messages flow through a 49-line `MessageBus` (bounded queue + backpressure) into the kernel; below the kernel, tools, skills, and providers form the capability layer. **All cross-layer communication goes through explicit interfaces — no implicit global state.**

## Execution kernel

### Turn state machine (`agent/turn_orchestrator.py`)

Every conversation turn is a deterministic state transition:

```
RESTORE → COMPACT → COMMAND → BUILD → RUN → SAVE → RESPOND → DONE
```

The state machine is extracted from `AgentLoop`; dependencies are injected explicitly via `TurnDeps`. Restore and compaction happen *before* the conversation proceeds, guaranteeing crash recovery and non-rotting context.

### ReAct loop (`agent/runner.py`)

A fixed Thought → Action → Observation loop — the **only** processing path in the system. The "Dumb Loop" philosophy: keep the loop dumb and transparent, leave intelligence to the model, push complexity to the edge.

### Plan-and-execute (`agent/planner.py`)

With the Planner enabled, the LLM first decomposes the task into ordered steps, then executes each step through the normal ReAct loop; failed steps trigger a replan carrying the failure reason forward. Plan snapshots (`plan_snapshot.py`) are persisted isolated from recovery checkpoints.

### Receipts and step acceptance (`tools/receipts.py` · `agent/step_acceptance.py`)

Every contract-tool call produces a **structured receipt** (status / result_excerpt / receipt). Whether a plan step is complete is decided by **deterministic rules** over receipt evidence; only when rules reject does an LLM verifier step in — step completion no longer depends on model self-reporting.

### Call ledger (`ledger/call_ledger.py`)

All LLM calls within a turn are accounted by purpose (executor / planner / replan / reflection); `turn_budget` uses this to bound per-turn resource consumption and prevent runaway loops.

### Context governance (`agent/context_governor.py`)

A pluggable strategy chain compresses the context back into the window before each LLM call: Snip (crop oldest segments, hand them to the Consolidator) → Microcompact (compress old tool results) → orphan governance (keep the tool-result message sequence structurally legal) → AutoCompact (proactively compress idle sessions). Third-party strategies register via the `erza.context_strategies` entry point.

### Reflection and Dream (`agent/reflection.py` · `memory/dream.py`)

Failures — or every N iterations — trigger a one-sentence lesson written to `reflections.jsonl`; **Dream** distills new summaries and reflections into candidate facts when idle (`dream_trigger.py`, fires 5 minutes after the user goes quiet, complementing the cron floor), feeding them into the governed memory lifecycle. The goal is cross-turn learning: never repeat the same mistake.

## State and memory

| Layer | Carrier | Responsibility |
|----|------|------|
| Short-term session | `session/` | Active conversation context, atomic writes (temp file + fsync + rename), crash-safe |
| Compacted archive | `memory/history.jsonl` | Append-only history summaries with cursors, maintained by the Consolidator |
| Long-term knowledge | `memory/structured/memory.db` | **Single SQLite fact store** (`memory/repository.py`, fail-closed health); only `memory/lifecycle.py` may promote/replace/revoke/expire records, all changes in one transaction |
| Lessons | `memory/reflections.jsonl` | Failure and periodic reflections |
| Version history | `utils/` GitStore (embedded Git) | Every long-term file change is diffable and rollback-able |

The only path for memory into a prompt is **deterministic recall**: only active facts matching exact scopes are recalled; candidate facts and historical archives are never injected wholesale. Legacy JSONL journals serve only as migration input (`memory/jsonl_import.py`).

## Capability layer

### Tool system (`tools/`, 25+ built-ins)

`ToolRegistry` with dynamic registration and aliases, `pkgutil` auto-discovery; every tool carries an explicit JSON Schema and executes under the security layer.

| Category | Tools |
|------|------|
| Filesystem | `read_file` · `write_file` · `edit_file` · `apply_patch` · `list_dir` · `find_files` · `grep` |
| Execution | `exec` (optional sandbox, persistent sessions) · `write_stdin` · `list_exec_sessions` |
| Retrieval | `web_fetch` (URL → Markdown, SSRF-guarded + DNS-rebinding pinning) |
| Orchestration | `cron` · `long_task` · `execute_plan` · `activate_plan` |
| Subagents | `spawn` · `delegate` · `create_agent` |
| External | `mcp_*` (multi-server) · `message` (cross-channel) |
| Introspection | `self` (runtime state queries, allow-list gated) |

Three external capability paths, none touching the kernel: **MCP servers** (`tools/mcp_runtime.py`, connection stacks owned by the composition root), **skills** (Markdown + YAML frontmatter, injected on demand), and **Python entry-point plugins**.

### LLM providers (`providers/`)

A unified base class plus declarative `ProviderSpec` behavior flags (`force_string_content`, `normalize_tool_call_ids`, …) that eliminate per-vendor hardcoded branches:

- OpenAI-compatible (DeepSeek, OpenRouter, Moonshot, Azure, vLLM, Ollama, etc.)
- OpenAI Responses API (dedicated parsing path for GPT-5 / o-series)
- Anthropic (adaptive thinking and cache optimization)
- `FallbackProvider` automatic failover; runtime hot-switching driven by `ProviderSignature` fingerprints

## Edge surfaces

| Module | Responsibility |
|------|------|
| `channels/` (12.6k lines) | 5 IM adapters (Feishu/WeChat/WeCom/DingTalk/QQ) + WebSocket, unified `BaseChannel` interface, QR-code login (`QRCodeAuthHandler`), `allowFrom` admission allow-lists, all optional extras |
| `bus/` | 49-line async message bus, bounded queue + natural backpressure |
| `command/` | Slash-command router with priority / exact / prefix matching, including governed memory-management commands |
| `cron/` | Natural-language scheduled tasks, persisted, catch-up execution after restart |
| `api_compat/` | OpenAI-compatible HTTP API (`/v1/chat/completions` SSE streaming, `/v1/models`), optional `[api]` extra |
| `webui/` | Python gateway: HTTP/WebSocket routing, settings/channel/tool/memory management APIs; frontend is React 18 + Vite + TypeScript (~40k lines) |

## Security model

| Boundary | Mechanism |
|------|------|
| File access | Workspace path boundary (`security/workspace_policy.py`); boundary violations are hard policy errors the model cannot bypass with shell tricks |
| Shell execution | Optional `bwrap` sandbox, restricted env injection, exec_session config gating |
| Outbound HTTP | SSRF protection: transport-level hook blocking IP-literal targets, DNS-rebinding pinning (30s TTL), redirect re-validation (`security/network.py`) |
| Risk levels | `RiskLevel` on tools; high-risk tools behind approval gates and isolated checkpoints (`agent/tool_checkpoint.py`) |
| Channel admission | Per-channel `allowFrom` allow-lists |

The permission architecture is separated from the reasoning architecture: security checks are enforced at the tool-execution layer, never left to model discretion.

## Composition roots and entry points

All long-lived objects (bus, cron, session manager, MCP stacks) are created by composition roots and shut down in reverse order — the kernel never creates its own resources:

| Entry | Root | Use |
|------|--------|------|
| `erza gateway` | `composition/gateway.py` | Full gateway (channels + WebUI + API) |
| `erza agent` / `erza serve` | `composition/agent_app.py` | Headless terminal chat / pure API server |
| Python SDK | `Erza.from_config()` in `erza.py` | Programmatic embedding |

```python
from erza import Erza

bot = Erza.from_config()
result = await bot.run("Summarize this repo's architecture")
print(result.content, result.tools_used)
```

## Code map

~**70k lines** of Python (69.6k), ~**40k lines** of TypeScript in the WebUI, **253 test files / 85k lines** of tests:

| Package | Lines | Responsibility |
|----|------|------|
| `erza/channels/` | 12,644 | IM channel adapters and media handling |
| `erza/agent/` | 12,511 | Execution kernel: state machine, ReAct, planning, acceptance, context governance |
| `erza/tools/` | 8,963 | Built-in tools, registry, MCP runtime, sandbox |
| `erza/webui/` | 6,485 | Console gateway API (frontend at repo-root `webui/`) |
| `erza/memory/` | 6,163 | SQLite memory repository, lifecycle governance, Dream distillation |
| `erza/providers/` | 5,121 | Multi-provider abstraction and fallback chain |
| `erza/cli/` | 3,717 | Typer commands, terminal rendering, gateway runner |
| `erza/utils/` | 3,516 | Document parsing, media decoding, GitStore, atomic writes |
| `erza/skills/` | 2,105 | Built-in skill packages |
| `erza/session/` | 1,608 | Session persistence and goal state |
| `erza/config/` | 1,285 | Pydantic config models (camelCase/snake_case dual-compatible) |
| `erza/command/` | 1,258 | Slash-command router |
| `erza/security/` | 1,240 | Workspace boundary, SSRF, risk levels |
| `erza/cron/` | 1,014 | Scheduled-task service |
| `erza/composition/` | 573 | Composition roots (gateway / agent_app) |
| `erza/api_compat/` | 557 | OpenAI-compatible API |
| `erza/ledger/` | 431 | Call ledger and turn budget |
| `erza/bus/` | 141 | Message bus |
| `erza/erza.py` | SDK facade | `Erza.from_config().run()` |

## Quick start

```bash
# Install from source
git clone https://github.com/zhoulingquan/Erza.git
cd Erza
pip install -e .

# Optional extras
pip install -e ".[api,pdf,dev]"   # HTTP API / PDF parsing / tests
```

~30 pure-Python runtime dependencies (no native builds except lxml). Docker / Linux services / macOS LaunchAgent deployment are covered in [deployment.md](./docs/deployment.md).

**One command to start** — config and workspace are auto-initialized:

```bash
erza gateway
# → open http://127.0.0.1:8765
```

There is no LLM config on first start; set any OpenAI-compatible provider's API key in **Settings → Model Configuration** in the WebUI — it takes effect immediately, no restart needed.

```bash
erza agent            # CLI terminal chat (configure an LLM first)
erza serve            # OpenAI-compatible API only
erza onboard --wizard # Interactive setup wizard
```

The config file lives at `~/.erza/config.json` and supports `${VAR}` env substitution.

## Channel access

| Channel | Credentials | WebUI QR login |
|------|---------|---------------|
| WebSocket | Built-in WebUI, nothing to configure | — |
| Feishu | App ID + App Secret | ✓ |
| DingTalk | App Key + App Secret | ✓ |
| WeCom | Bot ID + Bot Secret | ✓ |
| WeChat | — | ✓ |
| QQ | App ID + App Secret | ✓ |

Channels are auto-discovered via `pkgutil` and extensible via entry-point plugins — see [channel-plugin-guide.md](./docs/channel-plugin-guide.md).

## Built-in skills

Markdown + YAML frontmatter, loaded on demand:

`cron` · `document-processing` · `github` · `long-goal` · `memory` · `my` · `skill-creator` · `summarize` · `tmux` · `update-setup` · `weather`

## Testing and quality

253 test files, 85k lines of tests covering every core module; `pytest-asyncio` auto mode + coverage; `ruff` static checks; CI runs a three-OS matrix.

```bash
pip install -e ".[dev]"
pytest
```

## Documentation

| Topic | Link |
|------|------|
| Quick start | [quick-start.md](./docs/quick-start.md) |
| Configuration | [configuration.md](./docs/configuration.md) |
| Channels | [chat-apps.md](./docs/chat-apps.md) |
| WebUI | [../webui/README.md](./webui/README.md) |
| CLI reference | [cli-reference.md](./docs/cli-reference.md) |
| Chat commands | [chat-commands.md](./docs/chat-commands.md) |
| OpenAI API | [openai-api.md](./docs/openai-api.md) |
| Deployment | [deployment.md](./docs/deployment.md) |
| Memory system | [memory.md](./docs/memory.md) |
| Python SDK | [python-sdk.md](./docs/python-sdk.md) |
| Channel plugins | [channel-plugin-guide.md](./docs/channel-plugin-guide.md) |

Full docs index: [docs/README.md](./docs/README.md).

## Contributing

PRs welcome. The codebase is deliberately readable — the kernel / governance / edge boundaries are clean; read the layer you're changing.

| Branch | Purpose |
|------|------|
| `main` | Stable releases |
| `nightly` | Experimental features |

See [CONTRIBUTING.md](./CONTRIBUTING.md).

## License

MIT — see [LICENSE](./LICENSE) and [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md).

---

<div align="center">

<em>Transparent kernel. Deterministic governance. Extensions at the edge.</em>

</div>
