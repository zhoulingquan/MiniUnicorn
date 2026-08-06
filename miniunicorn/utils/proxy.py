"""Proxy resolution helpers shared across network-facing code paths.

``httpx`` and ``requests`` (used by huggingface_hub) only honor
``HTTP_PROXY``/``HTTPS_PROXY`` env vars and never read the OS-level proxy
themselves. On Windows, local proxy clients (Clash, V2rayN, Shadowsocks, ...)
write their listening address into the registry ``Internet Settings``, which
browsers use; these helpers expose that value as a fallback and temporarily
inject it into the environment for third-party downloaders we cannot pass a
proxy to directly.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def system_proxy_url() -> str | None:
    """Return the OS-level proxy URL, or ``None`` when none is configured.

    Uses ``urllib.request.getproxies_registry()`` directly because
    ``getproxies()`` is ``getproxies_environment() or getproxies_registry()``
    — a mere ``NO_PROXY`` env var would short-circuit the registry lookup.
    """
    try:
        import urllib.request

        proxies = urllib.request.getproxies_registry()
    except Exception:
        return None
    for key in ("https", "http", "ftp"):
        url = proxies.get(key)
        if url:
            return url
    return None


def env_proxies_set() -> bool:
    """Return whether any proxy env var is already configured."""
    return any(os.environ.get(key) for key in _PROXY_ENV_KEYS)


@contextmanager
def env_proxy_fallback() -> Iterator[None]:
    """Temporarily expose the OS-level proxy via env vars if none are set.

    Third-party downloaders (huggingface_hub's ``snapshot_download``, ...)
    resolve proxies from the environment per request, so setting the vars
    around the call is enough. Explicit env config always wins; when no
    system proxy exists this is a no-op.
    """
    if env_proxies_set():
        yield
        return
    proxy = system_proxy_url()
    if proxy is None:
        yield
        return
    saved = {key: os.environ.get(key) for key in _PROXY_ENV_KEYS}
    os.environ["HTTP_PROXY"] = proxy
    os.environ["HTTPS_PROXY"] = proxy
    try:
        yield
    finally:
        for key in _PROXY_ENV_KEYS:
            if saved[key] is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = saved[key]
