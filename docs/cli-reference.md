# CLI Reference

| Command | Description |
|---------|-------------|
| `miniunicorn onboard` | Initialize config & workspace at `~/.miniunicorn/` |
| `miniunicorn onboard --wizard` | Launch the interactive onboarding wizard |
| `miniunicorn onboard -c <config> -w <workspace>` | Initialize or refresh a specific instance config and workspace |
| `miniunicorn agent -m "..."` | Chat with the agent |
| `miniunicorn agent -w <workspace>` | Chat against a specific workspace |
| `miniunicorn agent -w <workspace> -c <config>` | Chat against a specific workspace/config |
| `miniunicorn agent` | Interactive chat mode |
| `miniunicorn agent --no-markdown` | Show plain-text replies |
| `miniunicorn agent --logs` | Show runtime logs during chat |
| `miniunicorn serve` | Start the OpenAI-compatible API |
| `miniunicorn gateway` | Start the gateway |
| `miniunicorn gateway --runtime-mode <mode>` | Start the gateway in `lightweight` or `supervised` mode. `gateway` defaults to `supervised`. |
| `miniunicorn agent --runtime-mode <mode>` | Run the agent in `lightweight` or `supervised` mode. `agent` defaults to `lightweight`. |
| `miniunicorn serve --runtime-mode <mode>` | Start the OpenAI-compatible API in `lightweight` or `supervised` mode. `serve` defaults to `lightweight`. |
| `miniunicorn status` | Show status |
| `miniunicorn provider login openai-codex` | OAuth login for providers |
| `miniunicorn channels login <channel>` | Authenticate a channel interactively |
| `miniunicorn channels status` | Show channel status |
| `/dream` (in chat) | Submit a durable `DREAM` maintenance task through the TaskService; returns a task handle/status. Does not spawn an untracked coroutine. |

The `--runtime-mode` flag accepts `lightweight` or `supervised` and applies to
`gateway`, `agent`, and `serve`. Mode resolution precedence: CLI
`--runtime-mode` > env `MINIUNICORN_RUNTIME_MODE` > config `runtime.mode` >
launcher default (`gateway` → `supervised`, `agent`/`serve` → `lightweight`).

Interactive mode exits: `exit`, `quit`, `/exit`, `/quit`, `:q`, or `Ctrl+D`.
