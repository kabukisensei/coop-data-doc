"""Build/scan progress display (stderr-only, opt-in).

A thin wrapper over ``click.progressbar`` used by the CLI to show per-file
progress through the pipeline. It is deliberately decoupled from the
parsers: each parser/renderer accepts an optional ``on_file``/``on_node``
callback (a plain ``Callable``) and ticks it once per item — the parser
never decides how progress is rendered, preserving the "parsers don't
print" rule. Only the CLI constructs a Progress and supplies real ticks.

Progress is shown only when explicitly enabled (interactive stderr TTY,
not ``--quiet``). When disabled every method is a cheap no-op, so:
- CI / piped output stays clean (no control characters in logs), and
- generated artifacts are never affected — output goes to stderr only,
  so the byte-identical determinism contract is untouched.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import click

# A tick callback: called once per processed item. The optional argument is
# the item's label (e.g. file path), used only for display.
Tick = Callable[..., None]

_NOOP: Tick = lambda *_args, **_kwargs: None  # noqa: E731


def should_enable(quiet: bool) -> bool:
    """Progress is shown only for a human watching: not quiet, stderr a TTY."""
    if quiet:
        return False
    try:
        return bool(sys.stderr.isatty())
    except (AttributeError, ValueError):
        return False


class Progress:
    """Drives labelled progress bars/lines on stderr when enabled."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def line(self, message: str) -> None:
        """Print a one-off status line (only when enabled)."""
        if self.enabled:
            click.echo(message, err=True)

    @contextmanager
    def bar(self, label: str, total: int) -> Iterator[Tick]:
        """Yield a tick callable for a phase of ``total`` items.

        Disabled, or an empty phase, yields a no-op and renders nothing.
        The tick accepts an optional label argument it currently ignores
        (kept so callers can pass the current file path uniformly).
        """
        if not self.enabled or total <= 0:
            yield _NOOP
            return
        with click.progressbar(
            length=total,
            label=label,
            file=sys.stderr,
            show_eta=False,
        ) as bar:
            yield lambda *_args, **_kwargs: bar.update(1)
