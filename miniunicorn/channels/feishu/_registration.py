"""Feishu/Lark QR scan-to-create onboarding and registration flow.

Device-code flow: user scans a QR code with the Feishu/Lark mobile app and
the platform creates a fully configured bot application automatically. The
result is persisted into ``config.json`` via the instance helpers in
``_feishu_instances``.

Extracted from ``channel.py``. Symbols that live on ``channel.py`` (such as
``FeishuChannel``, ``fetch_feishu_app_identity``, ``FEISHU_AVAILABLE`` and
``_identity_timestamp``) are imported lazily inside functions to avoid a
top-level circular import — ``channel.py`` imports this module at load time
to re-export the registration API.
"""

from __future__ import annotations

import json
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from miniunicorn.channels.feishu._feishu_instances import (
    DEFAULT_INSTANCE_ID,
    feishu_instance_specs,
    update_feishu_instance_preserving_shape,
    upsert_feishu_instance,
)

if TYPE_CHECKING:
    pass

_LOGIN_CONSOLE = Console()

_ONBOARD_ACCOUNTS_URLS = {
    "feishu": "https://accounts.feishu.cn",
    "lark": "https://accounts.larksuite.com",
}
_REGISTRATION_PATH = "/oauth/v1/app/registration"
_ONBOARD_REQUEST_TIMEOUT_S = 10


def _accounts_base_url(domain: str) -> str:
    return _ONBOARD_ACCOUNTS_URLS.get(domain, _ONBOARD_ACCOUNTS_URLS["feishu"])


def _post_registration(base_url: str, body: dict[str, str]) -> dict:
    """POST form-encoded data to the registration endpoint, return parsed JSON.

    The registration endpoint returns JSON even on HTTP errors (e.g. poll
    returns authorization_pending as a 400). We always parse the body.
    """
    import httpx

    url = f"{base_url}{_REGISTRATION_PATH}"
    resp = httpx.post(
        url,
        data=body,
        timeout=_ONBOARD_REQUEST_TIMEOUT_S,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        return resp.json()
    except json.JSONDecodeError:
        resp.raise_for_status()
        return {}


def _init_registration(domain: str = "feishu") -> None:
    """Verify the environment supports client_secret auth. Raises RuntimeError if not."""
    base_url = _accounts_base_url(domain)
    res = _post_registration(base_url, {"action": "init"})
    methods = res.get("supported_auth_methods") or []
    if "client_secret" not in methods:
        raise RuntimeError(
            f"Feishu / Lark registration does not support client_secret auth. Supported: {methods}"
        )


def _begin_registration(domain: str = "feishu") -> dict:
    """Start the device-code flow. Returns device_code, qr_url, interval, expire_in."""
    base_url = _accounts_base_url(domain)
    res = _post_registration(
        base_url,
        {
            "action": "begin",
            "archetype": "PersonalAgent",
            "auth_method": "client_secret",
            "request_user_info": "open_id",
        },
    )
    device_code = res.get("device_code")
    if not device_code:
        raise RuntimeError("Feishu / Lark registration did not return a device_code")
    qr_url = res.get("verification_uri_complete", "")
    if not qr_url:
        raise RuntimeError("Feishu / Lark registration did not return a login URL")
    return {
        "device_code": device_code,
        "qr_url": qr_url,
        "interval": res.get("interval") or 5,
        "expire_in": res.get("expire_in") or 600,
    }


def _poll_registration(
    *,
    device_code: str,
    interval: int,
    expire_in: int,
    domain: str = "feishu",
) -> dict | None:
    """Poll until the user scans the QR code, or timeout/denial.

    Returns dict with app_id, app_secret, domain on success, None on failure.
    """
    deadline = time.monotonic() + expire_in
    current_domain = domain

    while time.monotonic() < deadline:
        try:
            res = poll_registration_once(device_code=device_code, domain=current_domain)
        except Exception:
            time.sleep(interval)
            continue

        current_domain = res.get("domain", current_domain)

        if res.get("status") == "succeeded":
            return {
                "app_id": res["app_id"],
                "app_secret": res["app_secret"],
                "domain": res.get("domain", current_domain),
            }

        if res.get("status") == "failed":
            _LOGIN_CONSOLE.print("[yellow]Authorization was cancelled or expired.[/yellow]")
            return None

        # authorization_pending or unknown — keep polling
        time.sleep(interval)

    _LOGIN_CONSOLE.print("[yellow]Authorization timed out.[/yellow]")
    return None


def poll_registration_once(
    *,
    device_code: str,
    domain: str = "feishu",
) -> dict:
    """Poll the Feishu/Lark device-code flow once.

    This non-blocking shape is used by WebUI. The CLI keeps using
    ``_poll_registration`` to wait in the terminal.
    """
    current_domain = domain
    base_url = _accounts_base_url(current_domain)
    res = _post_registration(
        base_url,
        {
            "action": "poll",
            "device_code": device_code,
            "tp": "ob_app",
        },
    )

    user_info = res.get("user_info") or {}
    tenant_brand = user_info.get("tenant_brand")
    if tenant_brand == "lark":
        current_domain = "lark"

    if res.get("client_id") and res.get("client_secret"):
        return {
            "status": "succeeded",
            "app_id": res["client_id"],
            "app_secret": res["client_secret"],
            "domain": current_domain,
        }

    error = res.get("error", "")
    if error in ("access_denied", "expired_token"):
        return {
            "status": "failed",
            "error": error,
            "domain": current_domain,
        }

    return {
        "status": "pending",
        "domain": current_domain,
    }


def _feishu_app_identity_key(app_id: str, domain: str) -> str:
    normalized_app_id = app_id.strip()
    normalized_domain = "lark" if domain.strip().lower() == "lark" else "feishu"
    return f"{normalized_domain}:{normalized_app_id}" if normalized_app_id else ""


def _saved_feishu_instance_identity_key(
    feishu_cfg: Any,
    defaults: dict[str, Any],
    instance_id: str,
) -> str:
    for spec in feishu_instance_specs(feishu_cfg, defaults):
        if spec.instance_id == instance_id:
            return _feishu_app_identity_key(
                str(spec.config.get("appId") or spec.config.get("app_id") or ""),
                str(spec.config.get("domain") or "feishu"),
            )
    return ""


def sync_saved_feishu_identity_boundary(
    *,
    instance_id: str,
    app_id: str,
    domain: str,
) -> bool:
    """Persist the Feishu app identity marker and clear access if it changed.

    WebUI connect normally handles this at save time. This startup check catches
    manual config edits so approved users do not accidentally carry over to a
    different Feishu/Lark app in the same local instance slot.
    """
    current_identity_key = _feishu_app_identity_key(app_id, domain)
    if not current_identity_key:
        return False

    # Lazy import to avoid circular dependency with channel.py.
    from miniunicorn.channels.feishu.channel import FeishuChannel
    from miniunicorn.config.loader import load_config, save_config

    full_config = load_config()
    feishu_cfg = getattr(full_config.channels, "feishu", None) or {}
    if not isinstance(feishu_cfg, dict):
        feishu_cfg = {}

    defaults = FeishuChannel.default_config()
    previous_identity_key = ""
    for spec in feishu_instance_specs(feishu_cfg, defaults):
        if spec.instance_id == instance_id:
            previous_identity_key = str(
                spec.config.get("identityKey") or spec.config.get("identity_key") or ""
            )
            break

    access_cleared = bool(previous_identity_key and previous_identity_key != current_identity_key)
    values: dict[str, Any] = {"identityKey": current_identity_key}
    if access_cleared:
        values["allowFrom"] = []
        values["allow_from"] = []

    if not previous_identity_key or access_cleared:
        feishu_cfg = update_feishu_instance_preserving_shape(
            feishu_cfg,
            defaults,
            instance_id,
            values,
        )
        setattr(full_config.channels, "feishu", feishu_cfg)
        save_config(full_config)

    return access_cleared


def save_registration_result(
    result: dict,
    *,
    instance_id: str = DEFAULT_INSTANCE_ID,
    name: str | None = None,
) -> None:
    """Persist a successful Feishu/Lark registration result to config.json."""
    # Lazy import to avoid circular dependency with channel.py.
    from miniunicorn.channels.feishu.channel import (
        FeishuChannel,
        fetch_feishu_app_identity,
    )
    from miniunicorn.config.loader import load_config, save_config

    full_config = load_config()
    feishu_cfg = getattr(full_config.channels, "feishu", None) or {}
    if not isinstance(feishu_cfg, dict):
        feishu_cfg = {}
    defaults = FeishuChannel.default_config()
    app_id = str(result["app_id"]).strip()
    domain = str(result.get("domain", "feishu") or "feishu").strip().lower()
    domain = "lark" if domain == "lark" else "feishu"
    previous_identity_key = _saved_feishu_instance_identity_key(feishu_cfg, defaults, instance_id)
    next_identity_key = _feishu_app_identity_key(app_id, domain)
    identity_changed = bool(previous_identity_key and previous_identity_key != next_identity_key)
    identity: dict[str, str] = {}
    with suppress(Exception):
        identity = fetch_feishu_app_identity(
            app_id,
            str(result["app_secret"]),
            domain,
        )
    values = {
        "name": name
        or ("miniunicorn" if instance_id == DEFAULT_INSTANCE_ID else f"miniunicorn {instance_id}"),
        "appId": app_id,
        "appSecret": result["app_secret"],
        "domain": domain,
        "identityKey": next_identity_key,
        "enabled": True,
        **identity,
    }
    if identity_changed:
        values["allowFrom"] = []
        values["allow_from"] = []
    feishu_cfg = upsert_feishu_instance(
        feishu_cfg,
        defaults,
        instance_id,
        values,
    )
    setattr(full_config.channels, "feishu", feishu_cfg)
    save_config(full_config)


def refresh_saved_feishu_identities(config: Any | None = None) -> bool:
    """Backfill missing Feishu assistant display identity in saved config.

    Existing users may already have working App ID/Secret credentials from
    older builds. Fetch identity only when an instance has credentials but no
    identity metadata at all, then persist the attempt so Settings does not hit
    Feishu on every render.
    """
    # Lazy import to avoid circular dependency with channel.py.
    from miniunicorn.channels.feishu.channel import (
        FEISHU_AVAILABLE,
        FeishuChannel,
        _identity_timestamp,
        fetch_feishu_app_identity,
    )

    if not FEISHU_AVAILABLE:
        return False

    from miniunicorn.config.loader import load_config, save_config

    full_config = config or load_config()
    feishu_cfg = getattr(full_config.channels, "feishu", None)
    defaults = FeishuChannel.default_config()
    specs = feishu_instance_specs(feishu_cfg, defaults)
    updated = False

    for spec in specs:
        instance = spec.config
        if (
            instance.get("displayName")
            or instance.get("avatarUrl")
            or instance.get("identityFetchedAt")
        ):
            continue

        app_id = str(instance.get("appId") or instance.get("app_id") or "").strip()
        app_secret = str(instance.get("appSecret") or instance.get("app_secret") or "").strip()
        if not app_id or not app_secret:
            continue

        identity = fetch_feishu_app_identity(
            app_id,
            app_secret,
            str(instance.get("domain") or "feishu"),
        )
        if not identity:
            identity = {"identityFetchedAt": _identity_timestamp()}

        feishu_cfg = update_feishu_instance_preserving_shape(
            feishu_cfg,
            defaults,
            spec.instance_id,
            identity,
        )
        updated = True

    if not updated:
        return False

    setattr(full_config.channels, "feishu", feishu_cfg)
    save_config(full_config)
    return True


def qr_register(
    *,
    initial_domain: str = "feishu",
) -> dict | None:
    """Run the Feishu / Lark scan-to-create QR registration flow.

    Returns on success:
        {
            "app_id": str,
            "app_secret": str,
            "domain": "feishu" | "lark",
        }

    Returns None on expected failures (network, auth denied, timeout).
    Unexpected errors (bugs, protocol regressions) propagate to the caller.
    """
    import httpx

    try:
        return _qr_register_inner(initial_domain=initial_domain)
    except (RuntimeError, OSError, json.JSONDecodeError, httpx.HTTPError) as exc:
        _LOGIN_CONSOLE.print(
            f"[yellow]Unable to start Feishu/Lark login:[/yellow] {escape(str(exc))}"
        )
        return None


def _print_qr_code(url: str) -> None:
    """Print QR code as ASCII art if qrcode package is available, otherwise print URL."""
    try:
        import qrcode as qr_lib

        _LOGIN_CONSOLE.print("\n[bold]Scan with Feishu or Lark[/bold]\n")
        qr = qr_lib.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        _LOGIN_CONSOLE.print()
    except ImportError:
        _LOGIN_CONSOLE.print()
        _LOGIN_CONSOLE.print(
            Panel.fit(Text(url), title="Open with Feishu or Lark", border_style="cyan")
        )
        _LOGIN_CONSOLE.print()


def _qr_register_inner(
    *,
    initial_domain: str,
) -> dict | None:
    """Run init → begin → poll. Raises on network/protocol errors."""
    _LOGIN_CONSOLE.print("[cyan]Preparing Feishu/Lark login...[/cyan]")
    _init_registration(initial_domain)
    begin = _begin_registration(initial_domain)

    _print_qr_code(begin["qr_url"])

    with _LOGIN_CONSOLE.status("Waiting for authorization in Feishu/Lark...", spinner="dots"):
        return _poll_registration(
            device_code=begin["device_code"],
            interval=begin["interval"],
            expire_in=begin["expire_in"],
            domain=initial_domain,
        )
