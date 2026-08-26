"""Optional OpenAI-compatible HTTP API for MiniUnicorn.

This package is only usable with the ``api`` extra installed
(``pip install miniunicorn-ai[api]``): it pulls in ``aiohttp``.
The ``miniunicorn serve`` CLI entry point imports it lazily and
prints an install hint when the extra is missing.
"""
