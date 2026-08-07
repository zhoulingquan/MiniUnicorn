# Deployment

## Runtime Topology

MiniUnicorn runs on a durable Runtime with two modes (see
[Configuration → Runtime](configuration.md#runtime)):

- **`lightweight`** — a single process. Default for the one-shot `agent` and
  `serve` commands, and suitable for dev/single-slot deployments.
- **`supervised`** — the default for the long-running `gateway` command: one
  Control Plane process plus Worker child processes (default `workerCount=3`,
  minimum `2`), each with `workerConcurrency=1` (fixed).

Select the mode with `--runtime-mode supervised` (or `lightweight`), the
`MINIUNICORN_RUNTIME_MODE` environment variable, or `runtime.mode` in
`config.json`. Resolution precedence is CLI > env > config > launcher default.

### Process ownership (supervised mode)

| Process | Owns |
|---------|------|
| Launcher / Supervisor | Child process lifecycle — start/stop/restart, backoff. |
| Control Plane | Ingress, Channels, Outbox sender, Cron enqueue. |
| Workers | Agent execution, Provider calls, tool execution (ToolGateway), session commit. |

### Graceful shutdown

The graceful-shutdown grace period is `runtime.shutdownGraceS` (default `60s`).
For Docker, set `stop_grace_period` to cover the grace period plus child-tree
termination:

- **`75s`** for supervised mode (60s grace + child-tree termination).
- **`30s`** for lightweight mode.

The supervisor drains in-flight work within the grace window, then terminates
the child process tree. Expired task leases and expired `SENDING` Outbox rows
are reclaimed on the next scan, so a stopped container never permanently wedges
a session or target queue.

## Docker

> [!TIP]
> The `-v ~/.miniunicorn:/home/miniunicorn/.miniunicorn` flag mounts your local config directory into the container, so your config and workspace persist across container restarts.
> The container runs as the non-root user `miniunicorn` (UID 1000) and reads config from `/home/miniunicorn/.miniunicorn`. Always mount your host config directory to `/home/miniunicorn/.miniunicorn`, not `/root/.miniunicorn`.
> If you get **Permission denied**, fix ownership on the host first: `sudo chown -R 1000:1000 ~/.miniunicorn`, or pass `--user $(id -u):$(id -g)` to match your host UID. Podman users can use `--userns=keep-id` instead.
>
> [!IMPORTANT]
> Official Docker usage currently means building from this repository with the included `Dockerfile`. Docker Hub images under third-party namespaces are not maintained or verified by the project; do not mount API keys or bot tokens into them unless you trust the publisher.

> [!IMPORTANT]
> The gateway and WebSocket channel default to `host: "127.0.0.1"` in `config.json` (set in `miniunicorn/config/schema.py`). Docker `-p` port forwarding cannot reach a container's loopback interface, so for the host or LAN to reach the exposed ports you must set both binds to `0.0.0.0` in `~/.miniunicorn/config.json` before starting the container:
>
> ```json
> {
>   "gateway":  { "host": "0.0.0.0" },
>   "channels": { "websocket": { "host": "0.0.0.0" } }
> }
> ```
>
> When `host` is `0.0.0.0`, the gateway refuses to start unless `token` or `tokenIssueSecret` is also configured on the WebSocket channel — see [`webui/README.md`](../webui/README.md) for details.

### Docker Compose

```bash
docker compose run --rm miniunicorn-cli onboard   # first-time setup
vim ~/.miniunicorn/config.json                     # add API keys
docker compose up -d miniunicorn-gateway           # start gateway
```

```bash
docker compose run --rm miniunicorn-cli agent -m "Hello!"   # run CLI
docker compose logs -f miniunicorn-gateway                   # view logs
docker compose down                                      # stop
```

### Docker

```bash
# Build the image
docker build -t miniunicorn .

# Initialize config (first time only)
docker run -v ~/.miniunicorn:/home/miniunicorn/.miniunicorn --rm miniunicorn onboard

# Edit config on host to add API keys
vim ~/.miniunicorn/config.json

# Run gateway (connects to enabled channels, e.g. Telegram/Discord/Mochat).
# Mirrors the security caps and port mappings declared in docker-compose.yml:
#   - `--cap-drop ALL --cap-add SYS_ADMIN` + unconfined apparmor/seccomp are required
#     when `tools.exec.sandbox: "bwrap"` is enabled (bwrap needs CAP_SYS_ADMIN for
#     user namespaces). Without them, `bwrap` exits with `clone3: Operation not permitted`.
#   - `-p 8765:8765` exposes the WebSocket channel / WebUI.
docker run \
  --cap-drop ALL --cap-add SYS_ADMIN \
  --security-opt apparmor=unconfined \
  --security-opt seccomp=unconfined \
  -v ~/.miniunicorn:/home/miniunicorn/.miniunicorn \
  -p 8765:8765 \
  miniunicorn gateway

# Or run a single command
docker run -v ~/.miniunicorn:/home/miniunicorn/.miniunicorn --rm miniunicorn agent -m "Hello!"
docker run -v ~/.miniunicorn:/home/miniunicorn/.miniunicorn --rm miniunicorn status
```

## Linux Service

Run the gateway as a systemd user service so it starts automatically and restarts on failure.

**1. Find the MiniUnicorn binary path:**

```bash
which miniunicorn   # e.g. /home/user/.local/bin/miniunicorn
```

**2. Create the service file** at `~/.config/systemd/user/miniunicorn-gateway.service` (replace `ExecStart` path if needed):

```ini
[Unit]
Description=miniunicorn Gateway
After=network.target

[Service]
Type=simple
# `gateway` defaults to supervised mode already; set --runtime-mode explicitly
# to pin it (values: lightweight | supervised), or use runtime.mode in config.json.
ExecStart=%h/.local/bin/miniunicorn gateway --runtime-mode supervised
Restart=always
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=%h

[Install]
WantedBy=default.target
```

**3. Enable and start:**

```bash
systemctl --user daemon-reload
systemctl --user enable --now miniunicorn-gateway
```

**Common operations:**

```bash
systemctl --user status miniunicorn-gateway        # check status
systemctl --user restart miniunicorn-gateway       # restart after config changes
journalctl --user -u miniunicorn-gateway -f        # follow logs
```

If you edit the `.service` file itself, run `systemctl --user daemon-reload` before restarting.

> **Note:** User services only run while you are logged in. To keep the gateway running after logout, enable lingering:
>
> ```bash
> loginctl enable-linger $USER
> ```

## macOS LaunchAgent

Use a LaunchAgent when you want `miniunicorn gateway` to stay online after you log in, without keeping a terminal open.

**1. Get the absolute `miniunicorn` path:**

```bash
which miniunicorn   # e.g. /Users/youruser/.local/bin/miniunicorn
```

Use that exact path in the plist. It keeps the Python environment from your install method.

**2. Create `~/Library/LaunchAgents/ai.miniunicorn.gateway.plist`:**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.miniunicorn.gateway</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/youruser/.local/bin/miniunicorn</string>
    <string>gateway</string>
    <string>--workspace</string>
    <string>/Users/youruser/.miniunicorn/workspace</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/youruser/.miniunicorn/workspace</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>

  <key>StandardOutPath</key>
  <string>/Users/youruser/.miniunicorn/logs/gateway.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/youruser/.miniunicorn/logs/gateway.error.log</string>
</dict>
</plist>
```

**3. Load and start it:**

```bash
mkdir -p ~/Library/LaunchAgents ~/.miniunicorn/logs
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.miniunicorn.gateway.plist
launchctl enable gui/$(id -u)/ai.miniunicorn.gateway
launchctl kickstart -k gui/$(id -u)/ai.miniunicorn.gateway
```

**Common operations:**

```bash
launchctl list | grep ai.miniunicorn.gateway
launchctl kickstart -k gui/$(id -u)/ai.miniunicorn.gateway   # restart
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.miniunicorn.gateway.plist
```

After editing the plist, run `launchctl bootout ...` and `launchctl bootstrap ...` again.

> **Note:** if startup fails with "address already in use", stop the manually started `miniunicorn gateway` process first.
