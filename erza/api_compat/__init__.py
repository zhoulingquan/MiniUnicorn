"""Optional OpenAI-compatible HTTP API for Erza.

This package is only usable with the ``api`` extra installed
(``pip install erza-ai[api]``): it pulls in ``aiohttp``.
The ``erza serve`` CLI entry point imports it lazily and
prints an install hint when the extra is missing.
"""
