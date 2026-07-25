# Deployment

## Docker

> [!TIP]
> The `-v ~/.miniUnicorn:/home/miniUnicorn/.miniUnicorn` flag mounts your local config directory into the container, so your config and workspace persist across container restarts.
> The container runs as the non-root user `miniUnicorn` (UID 1000) and reads config from `/home/miniUnicorn/.miniUnicorn`. Always mount your host config directory to `/home/miniUnicorn/.miniUnicorn`, not `/root/.miniUnicorn`.
> If you get **Permission denied**, fix ownership on the host first: `sudo chown -R 1000:1000 ~/.miniUnicorn`, or pass `--user $(id -u):$(id -g)` to match your host UID. Podman users can use `--userns=keep-id` instead.
>
> [!IMPORTANT]
> Official Docker usage currently means building from this repository with the included `Dockerfile`. Docker Hub images under third-party namespaces are not maintained or verified by the project; do not mount API keys or bot tokens into them unless you trust the publisher.

> [!IMPORTANT]
> The gateway and WebSocket channel default to `host: "127.0.0.1"` in `config.json` (set in `miniUnicorn/config/schema.py`). Docker `-p` port forwarding cannot reach a container's loopback interface, so for the host or LAN to reach the exposed ports you must set both binds to `0.0.0.0` in `~/.miniUnicorn/config.json` before starting the container:
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
docker compose run --rm miniUnicorn-cli onboard   # first-time setup
vim ~/.miniUnicorn/config.json                     # add API keys
docker compose up -d miniUnicorn-gateway           # start gateway
```

```bash
docker compose run --rm miniUnicorn-cli agent -m "Hello!"   # run CLI
docker compose logs -f miniUnicorn-gateway                   # view logs
docker compose down                                      # stop
```

### Docker

```bash
# Build the image
docker build -t miniUnicorn .

# Initialize config (first time only)
docker run -v ~/.miniUnicorn:/home/miniUnicorn/.miniUnicorn --rm miniUnicorn onboard

# Edit config on host to add API keys
vim ~/.miniUnicorn/config.json

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
  -v ~/.miniUnicorn:/home/miniUnicorn/.miniUnicorn \
  -p 8765:8765 \
  miniUnicorn gateway

# Or run a single command
docker run -v ~/.miniUnicorn:/home/miniUnicorn/.miniUnicorn --rm miniUnicorn agent -m "Hello!"
docker run -v ~/.miniUnicorn:/home/miniUnicorn/.miniUnicorn --rm miniUnicorn status
```

## Linux Service

Run the gateway as a systemd user service so it starts automatically and restarts on failure.

**1. Find the MiniUnicorn binary path:**

```bash
which MiniUnicorn   # e.g. /home/user/.local/bin/MiniUnicorn
```

**2. Create the service file** at `~/.config/systemd/user/miniUnicorn-gateway.service` (replace `ExecStart` path if needed):

```ini
[Unit]
Description=MiniUnicorn Gateway
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/bin/miniUnicorn gateway
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
systemctl --user enable --now miniUnicorn-gateway
```

**Common operations:**

```bash
systemctl --user status miniUnicorn-gateway        # check status
systemctl --user restart miniUnicorn-gateway       # restart after config changes
journalctl --user -u miniUnicorn-gateway -f        # follow logs
```

If you edit the `.service` file itself, run `systemctl --user daemon-reload` before restarting.

> **Note:** User services only run while you are logged in. To keep the gateway running after logout, enable lingering:
>
> ```bash
> loginctl enable-linger $USER
> ```

## macOS LaunchAgent

Use a LaunchAgent when you want `miniUnicorn gateway` to stay online after you log in, without keeping a terminal open.

**1. Get the absolute `MiniUnicorn` path:**

```bash
which MiniUnicorn   # e.g. /Users/youruser/.local/bin/MiniUnicorn
```

Use that exact path in the plist. It keeps the Python environment from your install method.

**2. Create `~/Library/LaunchAgents/ai.miniUnicorn.gateway.plist`:**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.miniUnicorn.gateway</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/youruser/.local/bin/MiniUnicorn</string>
    <string>gateway</string>
    <string>--workspace</string>
    <string>/Users/youruser/.miniUnicorn/workspace</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/youruser/.miniUnicorn/workspace</string>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>

  <key>StandardOutPath</key>
  <string>/Users/youruser/.miniUnicorn/logs/gateway.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/youruser/.miniUnicorn/logs/gateway.error.log</string>
</dict>
</plist>
```

**3. Load and start it:**

```bash
mkdir -p ~/Library/LaunchAgents ~/.miniUnicorn/logs
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.miniUnicorn.gateway.plist
launchctl enable gui/$(id -u)/ai.miniUnicorn.gateway
launchctl kickstart -k gui/$(id -u)/ai.miniUnicorn.gateway
```

**Common operations:**

```bash
launchctl list | grep ai.miniUnicorn.gateway
launchctl kickstart -k gui/$(id -u)/ai.miniUnicorn.gateway   # restart
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.miniUnicorn.gateway.plist
```

After editing the plist, run `launchctl bootout ...` and `launchctl bootstrap ...` again.

> **Note:** if startup fails with "address already in use", stop the manually started `miniUnicorn gateway` process first.
