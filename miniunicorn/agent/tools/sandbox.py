"""Sandbox backends for shell command execution.

To add a new backend, implement a function with the signature:
    _wrap_<name>(command: str, workspace: str, cwd: str) -> str
and register it in _BACKENDS below.
"""

import shlex
from pathlib import Path

from miniunicorn.config.paths import get_media_dir


def _bwrap(
    command: str,
    workspace: str,
    cwd: str,
    unshare_net: bool = False,
) -> str:
    """Wrap command in a bubblewrap sandbox (requires bwrap in container).

    Only the workspace is bind-mounted read-write; its parent dir (which holds
    config.json) is hidden behind a fresh tmpfs.  The media directory is
    bind-mounted read-only so exec commands can read uploaded attachments.

    注意:默认 ``unshare_net=False`` 不隔离网络,因为很多命令需要联网(例如
    包管理器、git fetch 等)。仅当 ``unshare_net=True`` 时,才会加入
    ``--unshare-net`` 实现网络隔离;生产环境如需更严格隔离可启用该选项。
    """
    ws = Path(workspace).resolve()
    media = get_media_dir().resolve()

    try:
        sandbox_cwd = str(ws / Path(cwd).resolve().relative_to(ws))
    except ValueError:
        sandbox_cwd = str(ws)

    required = ["/usr"]
    optional = ["/bin", "/lib", "/lib64", "/etc/alternatives",
                "/etc/ssl/certs", "/etc/resolv.conf", "/etc/ld.so.cache"]

    args = ["bwrap", "--new-session", "--die-with-parent"]
    # 可选网络隔离:仅在显式启用时切断沙箱网络,避免影响默认联网命令
    if unshare_net:
        args.append("--unshare-net")
    for p in required:
        args += ["--ro-bind", p, p]
    for p in optional:
        args += ["--ro-bind-try", p, p]
    args += [
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        "--tmpfs", str(ws.parent),        # mask config dir
        "--dir", str(ws),                 # recreate workspace mount point
        "--bind", str(ws), str(ws),
        "--ro-bind-try", str(media), str(media),  # read-only access to media
        "--chdir", sandbox_cwd,
        "--", "sh", "-c", command,
    ]
    return shlex.join(args)


_BACKENDS = {"bwrap": _bwrap}


def wrap_command(
    sandbox: str,
    command: str,
    workspace: str,
    cwd: str,
    unshare_net: bool = False,
) -> str:
    """Wrap *command* using the named sandbox backend.

    ``unshare_net`` 仅对支持网络隔离的后端(如 bwrap)生效,默认为 ``False``
    以保持向后兼容(多数命令需要联网,不应默认切断)。
    """
    if backend := _BACKENDS.get(sandbox):
        return backend(command, workspace, cwd, unshare_net=unshare_net)
    raise ValueError(f"Unknown sandbox backend {sandbox!r}. Available: {list(_BACKENDS)}")
