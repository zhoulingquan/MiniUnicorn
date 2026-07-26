"""Terminal rendering helpers (extracted from cli/commands.py).

This module owns the prompt_toolkit / Rich output path: spinner-aware
progress lines, reasoning buffers, interactive replies, and the safe
FileHistory subclass.

A few functions are called by tests via ``unittest.mock.patch`` on the
``miniUnicorn.cli.commands`` module namespace (e.g.
``commands._print_cli_reasoning``).  Because those patches replace the
attribute on ``commands`` (not on this module), the call sites inside
this module that need to honour such patches look the dependency up
through ``commands.<name>`` at call time (late binding).  This keeps the
public patch surface stable without changing any business logic.

The mutable prompt_toolkit session state (``_PROMPT_SESSION`` and
``_SAVED_TERM_ATTRS``) lives on the ``commands`` module so that
``patch("miniUnicorn.cli.commands._PROMPT_SESSION", ...)`` continues to
work; the helpers here reach it via ``commands._PROMPT_SESSION``.
"""

import os
import select
import sys
from contextlib import nullcontext, suppress
from typing import Any

from prompt_toolkit import print_formatted_text
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
# 重新导出到 commands 模块,供 _read_interactive_input_async 通过
# commands.patch_stdout() 走 late binding,使测试 monkeypatch 生效。
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from miniUnicorn import __logo__
from miniUnicorn.cli.stream import StreamRenderer, ThinkingSpinner

console = Console()
EXIT_COMMANDS = {"exit", "quit", "/exit", "/quit", ":q"}
_REASONING_SENTENCE_ENDINGS = (".", "!", "?", "。", "！", "？")
_REASONING_FLUSH_CHARS = 60


def _sanitize_surrogates(text: str) -> str:
    """Reconstruct surrogate pairs into real characters; replace lone surrogates.

    On Windows, console input may produce lone surrogate code points (e.g.
    ``\\ud83d\\udc08`` for U+1F408).  Round-tripping through UTF-16 reconstructs
    paired surrogates into their actual characters and replaces unpaired ones
    with U+FFFD.
    """
    return text.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="replace")


class SafeFileHistory(FileHistory):
    """FileHistory subclass that sanitizes surrogate characters on write.

    On Windows, special Unicode input (emoji, mixed-script) can produce
    surrogate characters that crash prompt_toolkit's file write.
    See issue #2846.
    """

    def store_string(self, string: str) -> None:
        super().store_string(_sanitize_surrogates(string))


def _flush_pending_tty_input() -> None:
    """Drop unread keypresses typed while the model was generating output."""
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
    except Exception:
        return

    with suppress(Exception):
        import termios

        termios.tcflush(fd, termios.TCIFLUSH)
        return

    with suppress(Exception):
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            if not os.read(fd, 4096):
                break


def _restore_terminal() -> None:
    """Restore terminal to its original state (echo, line buffering, etc.)."""
    from miniUnicorn.cli import commands

    if commands._SAVED_TERM_ATTRS is None:
        return
    with suppress(Exception):
        import termios

        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, commands._SAVED_TERM_ATTRS)


def _init_prompt_session() -> None:
    """Create the prompt_toolkit session with persistent file history.

    The session is stored on ``commands._PROMPT_SESSION`` (and the saved
    termios state on ``commands._SAVED_TERM_ATTRS``) so that test patches
    on those attributes are honoured by ``_read_interactive_input_async``
    below.  ``PromptSession`` is likewise resolved through ``commands``
    so that ``patch("miniUnicorn.cli.commands.PromptSession", ...)``
    takes effect.
    """
    from miniUnicorn.cli import commands

    # Save terminal state so we can restore it on exit
    with suppress(Exception):
        import termios

        commands._SAVED_TERM_ATTRS = termios.tcgetattr(sys.stdin.fileno())

    from miniUnicorn.config.paths import get_cli_history_path

    history_file = get_cli_history_path()
    history_file.parent.mkdir(parents=True, exist_ok=True)

    commands._PROMPT_SESSION = commands.PromptSession(
        history=SafeFileHistory(str(history_file)),
        enable_open_in_editor=False,
        multiline=False,  # Enter submits (single line mode)
    )


def _make_console() -> Console:
    return Console(file=sys.stdout)


def _render_interactive_ansi(render_fn) -> str:
    """Render Rich output to ANSI so prompt_toolkit can print it safely."""
    ansi_console = Console(
        force_terminal=sys.stdout.isatty(),
        color_system=console.color_system or "standard",
        width=console.width,
    )
    with ansi_console.capture() as capture:
        render_fn(ansi_console)
    return capture.get()


def _print_agent_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
    show_header: bool = True,
) -> None:
    """Render assistant response with consistent terminal styling."""
    console = _make_console()
    content = response or ""
    body = _response_renderable(content, render_markdown, metadata)
    if show_header:
        console.print()
        console.print(f"[cyan]{__logo__} MiniUnicorn[/cyan]")
    console.print(body)
    console.print()


def _response_renderable(content: str, render_markdown: bool, metadata: dict | None = None):
    """Render plain-text command output without markdown collapsing newlines."""
    if not render_markdown:
        return Text(content)
    if (metadata or {}).get("render_as") == "text":
        return Text(content)
    return Markdown(content)


async def _print_interactive_line(text: str) -> None:
    """Print async interactive updates with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        ansi = _render_interactive_ansi(
            lambda c: c.print(f"  [dim]↳ {text}[/dim]")
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


async def _print_interactive_response(
    response: str,
    render_markdown: bool,
    metadata: dict | None = None,
) -> None:
    """Print async interactive replies with prompt_toolkit-safe Rich styling."""
    def _write() -> None:
        content = response or ""
        ansi = _render_interactive_ansi(
            lambda c: (
                c.print(),
                c.print(f"[cyan]{__logo__} MiniUnicorn[/cyan]"),
                c.print(_response_renderable(content, render_markdown, metadata)),
                c.print(),
            )
        )
        print_formatted_text(ANSI(ansi), end="")

    await run_in_terminal(_write)


def _print_cli_progress_line(text: str, thinking: ThinkingSpinner | None, renderer: StreamRenderer | None = None) -> None:
    """Print a CLI progress line, pausing the spinner if needed."""
    if not text.strip():
        return
    target = renderer.console if renderer else console
    pause = renderer.pause_spinner() if renderer else (thinking.pause() if thinking else nullcontext())
    with pause:
        if renderer:
            renderer.ensure_header()
        target.print(f"  [dim]↳ {text}[/dim]")


class _ReasoningBuffer:
    def __init__(self) -> None:
        self._text = ""

    def add(self, text: str) -> str | None:
        if not text:
            return None
        self._text += text
        if self._should_flush(text):
            return self.flush()
        return None

    def flush(self) -> str | None:
        text = self._text.strip()
        self._text = ""
        return text or None

    def clear(self) -> None:
        self._text = ""

    def _should_flush(self, text: str) -> bool:
        stripped = text.rstrip()
        return (
            "\n" in text
            or stripped.endswith(_REASONING_SENTENCE_ENDINGS)
            or len(self._text) >= _REASONING_FLUSH_CHARS
        )


def _print_cli_reasoning(text: str, thinking: ThinkingSpinner | None, renderer: StreamRenderer | None = None) -> None:
    """Print reasoning/thinking content in a distinct style."""
    if not text.strip():
        return
    target = renderer.console if renderer else console
    pause = renderer.pause_spinner() if renderer else (thinking.pause() if thinking else nullcontext())
    with pause:
        if renderer:
            renderer.ensure_header()
        target.print(f"[dim italic]✻ {text}[/dim italic]")


def _flush_cli_reasoning(
    reasoning_buffer: "_ReasoningBuffer",
    thinking: ThinkingSpinner | None,
    renderer: StreamRenderer | None = None,
) -> None:
    """Flush any buffered reasoning text and print it via ``_print_cli_reasoning``.

    Resolves ``_print_cli_reasoning`` through the ``commands`` module so that
    ``patch("miniUnicorn.cli.commands._print_cli_reasoning", ...)`` continues
    to take effect after the split.
    """
    from miniUnicorn.cli import commands

    text = reasoning_buffer.flush()
    if text:
        commands._print_cli_reasoning(text, thinking, renderer)


async def _print_interactive_progress_line(text: str, thinking: ThinkingSpinner | None, renderer: StreamRenderer | None = None) -> None:
    """Print an interactive progress line, pausing the spinner if needed.

    Resolves ``_print_interactive_line`` through the ``commands`` module so
    that ``patch("miniUnicorn.cli.commands._print_interactive_line", ...)``
    continues to take effect after the split.
    """
    from miniUnicorn.cli import commands

    if not text.strip():
        return
    if renderer:
        with renderer.pause_spinner():
            renderer.ensure_header()
            renderer.console.print(f"  [dim]↳ {text}[/dim]")
    else:
        with thinking.pause() if thinking else nullcontext():
            await commands._print_interactive_line(text)


async def _maybe_print_interactive_progress(
    msg: Any,
    thinking: ThinkingSpinner | None,
    channels_config: Any,
    renderer: StreamRenderer | None = None,
    reasoning_buffer: "_ReasoningBuffer | None" = None,
) -> bool:
    """Render an interactive progress / reasoning / retry-wait frame.

    The chained helpers (``_print_interactive_progress_line``,
    ``_flush_cli_reasoning``, ``_print_cli_reasoning``) are looked up through
    the ``commands`` module so that ``patch("miniUnicorn.cli.commands.<name>",
    ...)`` continues to take effect after the split.
    """
    from miniUnicorn.cli import commands

    metadata = msg.metadata or {}
    if metadata.get("_retry_wait"):
        await commands._print_interactive_progress_line(msg.content, thinking, renderer)
        return True

    if not metadata.get("_progress"):
        return False

    reasoning_buffer = reasoning_buffer or _ReasoningBuffer()

    if metadata.get("_reasoning_end"):
        if channels_config and not channels_config.show_reasoning:
            reasoning_buffer.clear()
        else:
            commands._flush_cli_reasoning(reasoning_buffer, thinking, renderer)
        return True

    is_tool_hint = metadata.get("_tool_hint", False)
    is_reasoning = metadata.get("_reasoning", False) or metadata.get("_reasoning_delta", False)
    if is_reasoning:
        if channels_config and not channels_config.show_reasoning:
            reasoning_buffer.clear()
            return True
        text = reasoning_buffer.add(msg.content)
        if text:
            commands._print_cli_reasoning(text, thinking, renderer)
        return True
    if channels_config and is_tool_hint and not channels_config.send_tool_hints:
        return True
    if channels_config and not is_tool_hint and not channels_config.send_progress:
        return True

    await commands._print_interactive_progress_line(msg.content, thinking, renderer)
    return True


def _is_exit_command(command: str) -> bool:
    """Return True when input should end interactive chat."""
    return command.lower() in EXIT_COMMANDS


async def _read_interactive_input_async() -> str:
    """Read user input using prompt_toolkit (handles paste, history, display).

    prompt_toolkit natively handles:
    - Multiline paste (bracketed paste mode)
    - History navigation (up/down arrows)
    - Clean display (no ghost characters or artifacts)

    Both ``_PROMPT_SESSION`` and ``patch_stdout`` are resolved through the
    ``commands`` module so that
    ``patch("miniUnicorn.cli.commands._PROMPT_SESSION", ...)`` /
    ``patch("miniUnicorn.cli.commands.patch_stdout", ...)`` continue to
    take effect after the split.
    """
    from miniUnicorn.cli import commands

    if commands._PROMPT_SESSION is None:
        raise RuntimeError("Call _init_prompt_session() first")
    try:
        with commands.patch_stdout():
            return await commands._PROMPT_SESSION.prompt_async(
                HTML("<b fg='ansiblue'>You:</b> "),
            )
    except EOFError as exc:
        raise KeyboardInterrupt from exc
