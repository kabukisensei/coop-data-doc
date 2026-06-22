"""Interactive resolution prompts (Module 4).

The ONLY module besides cli.py that may touch the terminal. Every answer
is persisted by the caller immediately, so Ctrl-C never loses progress.
"""

from __future__ import annotations

import sys

import questionary

from coop_data_doc.graph.model import Node
from coop_data_doc.linker.cache import CacheEntry

EXTERNAL_CHOICE = "__external__"
SKIP_CHOICE = "__skip__"


class TerminalUnavailable(RuntimeError):
    """No usable interactive terminal — e.g. run as a subprocess of another program,
    or on Windows with no console screen buffer. Callers degrade to non-interactive
    resolution instead of crashing the build."""


def _is_no_terminal_error(exc: BaseException) -> bool:
    """True for the errors questionary/prompt_toolkit raise when there is no usable
    console: questionary's no-TTY ``OSError`` and prompt_toolkit's Windows
    ``NoConsoleScreenBufferError`` (raised when launched without a console buffer,
    e.g. as a child process). Matched by name so we don't import prompt_toolkit."""
    return isinstance(exc, OSError) or type(exc).__name__ == "NoConsoleScreenBufferError"


def print_group_header(model_name: str, count: int) -> None:
    """Stderr banner shown once per semantic model during a session."""
    print(f"\n── {model_name} — {count} unresolved table(s) ──", file=sys.stderr)


def prompt_resolution(pbi_node: Node, source_desc: str, candidates: list[tuple[str, float]]) -> CacheEntry:
    """Ask the user to map one Power BI table to a SQL object; returns
    the chosen CacheEntry. Raises KeyboardInterrupt on Ctrl-C/EOF.
    """
    choices: list = [
        questionary.Choice(title=f"{node_id}   ({score:.2f})", value=node_id)
        for node_id, score in candidates[:10]
    ]
    choices.append(questionary.Separator())
    choices.append(
        questionary.Choice(title="🌐 Mark as external source (not in these repos)", value=EXTERNAL_CHOICE)
    )
    choices.append(questionary.Choice(title="⏭  Skip for now", value=SKIP_CHOICE))

    try:
        answer = questionary.select(
            f"Map Power BI table '{pbi_node.name}' (source: {source_desc}) to:",
            choices=choices,
        ).ask()
    except Exception as exc:  # noqa: BLE001 — re-raised below unless it's a no-terminal error
        if _is_no_terminal_error(exc):
            raise TerminalUnavailable(str(exc)) from exc
        raise

    if answer is None:  # Ctrl-C / EOF
        raise KeyboardInterrupt
    if answer == EXTERNAL_CHOICE:
        return CacheEntry(target=None, method="external")
    if answer == SKIP_CHOICE:
        return CacheEntry(target=None, method="skip")
    return CacheEntry(target=answer, method="interactive")
