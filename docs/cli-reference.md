# CLI Reference

| Command | Description |
|---------|-------------|
| `erza onboard` | Initialize config & workspace at `~/.erza/` |
| `erza onboard --wizard` | Launch the interactive onboarding wizard |
| `erza onboard -c <config> -w <workspace>` | Initialize or refresh a specific instance config and workspace |
| `erza agent -m "..."` | Chat with the agent |
| `erza agent -w <workspace>` | Chat against a specific workspace |
| `erza agent -w <workspace> -c <config>` | Chat against a specific workspace/config |
| `erza agent` | Interactive chat mode |
| `erza agent --no-markdown` | Show plain-text replies |
| `erza agent --logs` | Show runtime logs during chat |
| `erza serve` | Start the OpenAI-compatible API |
| `erza gateway` | Start the gateway |
| `erza status` | Show status |
| `erza channels login <channel>` | Authenticate a channel interactively |
| `erza channels status` | Show channel status |

Interactive mode exits: `exit`, `quit`, `/exit`, `/quit`, `:q`, or `Ctrl+D`.
