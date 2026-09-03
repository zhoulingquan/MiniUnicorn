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
| `miniunicorn status` | Show status |
| `miniunicorn channels login <channel>` | Authenticate a channel interactively |
| `miniunicorn channels status` | Show channel status |

Interactive mode exits: `exit`, `quit`, `/exit`, `/quit`, `:q`, or `Ctrl+D`.
