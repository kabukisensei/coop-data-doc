"""WizardIO — UI/transport abstraction for the coop-data-doc setup wizard.

The wizard needs to run in two modes:

1. Terminal mode (`questionary`) for direct human use.
2. JSONL line mode for agent/bridge callers: one prompt per line, caller responds
   with one answer per line.

This module defines the protocol and both implementations. Nothing here imports
questionary, so the JSONL transport has no terminal dependency.
"""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, TextIO

import questionary

MAX_JSONL_LINE = 1024 * 1024
_CHOICE_PREVIEW_LIMIT = 8
_CHOICE_VALUE_PREVIEW_LIMIT = 80


class WizardProtocolError(ValueError):
    """Malformed or contradictory bridge input (not a user cancellation)."""


@dataclass
class Choice:
    """A single checkbox/select option."""

    label: str
    value: str
    checked: bool = False


@dataclass
class Prompt:
    """One prompt emitted by the wizard. The caller must send back an Answer
    with the same ``id``."""

    id: str
    kind: Literal["text", "path", "confirm", "select", "checkbox", "notice"]
    message: str
    default: str | bool | list[str] | None = None
    choices: list[Choice] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "type": "prompt",
            "id": self.id,
            "kind": self.kind,
            "message": self.message,
            "default": self.default,
            "choices": [{"label": c.label, "value": c.value, "checked": c.checked} for c in self.choices],
        }


@dataclass
class Event:
    """Non-prompt event emitted by the wizard."""

    type: Literal["notice", "progress", "complete", "error", "cancelled"]
    id: str | None = None
    message: str | None = None
    data: dict | None = None

    def to_json(self) -> dict:
        out: dict = {"type": self.type}
        if self.id is not None:
            out["id"] = self.id
        if self.message is not None:
            out["message"] = self.message
        if self.data is not None:
            out["data"] = self.data
        return out


class WizardIO(ABC):
    """Abstract UI for the setup wizard. Implementations are terminal (questionary)
    or JSONL line-oriented (for agent bridges)."""

    @abstractmethod
    def text(self, prompt_id: str, message: str, default: str = "") -> str:
        """Ask for free-text input; return the answer string."""

    @abstractmethod
    def path(self, prompt_id: str, message: str, default: str = "") -> str:
        """Ask for a directory path; return the answer string."""

    @abstractmethod
    def confirm(self, prompt_id: str, message: str, default: bool = False) -> bool:
        """Ask a yes/no question; return True for yes."""

    @abstractmethod
    def select(self, prompt_id: str, message: str, choices: list[Choice], default: str | None = None) -> str:
        """Single-select from choices; return the selected value."""

    @abstractmethod
    def checkbox(self, prompt_id: str, message: str, choices: list[Choice]) -> list[str]:
        """Multi-select from choices; return selected values."""

    @abstractmethod
    def notice(self, message: str) -> None:
        """Show a non-interactive notice."""


class QuestionaryWizardIO(WizardIO):
    """Terminal implementation backed by questionary.

    Reads the ``questionary`` module via the module-level name so tests can
    monkeypatch ``wizard_io.questionary`` (as they previously patched
    ``wizard.questionary``).
    """

    def __init__(self) -> None:
        self._q = questionary

    def _ask(self, prompt) -> object:
        try:
            answer = prompt.ask()
        except EOFError as exc:
            raise KeyboardInterrupt from exc
        if answer is None:
            raise KeyboardInterrupt
        return answer

    def text(self, prompt_id: str, message: str, default: str = "") -> str:
        return str(self._ask(self._q.text(message, default=default))).strip()

    def path(self, prompt_id: str, message: str, default: str = "") -> str:
        return str(self._ask(self._q.path(message, default=default, only_directories=True))).strip()

    def confirm(self, prompt_id: str, message: str, default: bool = False) -> bool:
        return bool(self._ask(self._q.confirm(message, default=default, auto_enter=False)))

    def select(self, prompt_id: str, message: str, choices: list[Choice], default: str | None = None) -> str:
        q_choices = [self._q.Choice(c.label, c.value) for c in choices]
        return str(self._ask(self._q.select(message, choices=q_choices, default=default)))

    def checkbox(self, prompt_id: str, message: str, choices: list[Choice]) -> list[str]:
        q_choices = [self._q.Choice(c.label, c.value, checked=c.checked) for c in choices]
        selected = self._ask(self._q.checkbox(message, choices=q_choices))
        return list(selected) if selected else []

    def notice(self, message: str) -> None:
        print(message, file=sys.stderr)


class JsonlWizardIO(WizardIO):
    """Line-oriented JSON implementation for agent bridges.

    Emits one prompt or event per line on ``out_stream``; reads answers as JSONL
    from ``in_stream``. The process stays alive for the whole wizard session.
    The first line on ``out_stream`` is always a ``hello`` event carrying the
    wire-protocol version, so a bridge can verify compatibility before the first
    prompt.
    """

    PROTOCOL_VERSION = "1.1"

    def __init__(self, in_stream: TextIO, out_stream: TextIO) -> None:
        self._in = in_stream
        self._out = out_stream
        self._counter = 0
        self._emit({"type": "hello", "protocol_version": self.PROTOCOL_VERSION})

    def _next_id(self, kind: str) -> str:
        self._counter += 1
        return f"{kind}_{self._counter}"

    def _emit(self, obj: dict) -> None:
        self._out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self._out.flush()

    def _read_answer(self, prompt_id: str) -> dict:
        # +2 so a full MAX-length line plus its LF fits in one read; the LF is
        # then stripped before the length check (the limit covers content only).
        line = self._in.readline(MAX_JSONL_LINE + 2)
        if not line:
            raise KeyboardInterrupt
        line = line.removesuffix("\n")
        if len(line) > MAX_JSONL_LINE:
            raise WizardProtocolError("JSONL answer exceeds 1 MiB")
        try:
            answer = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WizardProtocolError(f"Invalid JSON answer: {exc}") from exc
        if not isinstance(answer, dict):
            raise WizardProtocolError("JSONL answer must be an object")
        if answer.get("id") != prompt_id:
            raise WizardProtocolError(f"Answer id mismatch: expected {prompt_id!r}, got {answer.get('id')!r}")
        if answer.get("cancelled") is True:
            raise KeyboardInterrupt
        return answer

    def _prompt(self, prompt: Prompt) -> dict:
        self._emit(prompt.to_json())
        return self._read_answer(prompt.id)

    def text(self, prompt_id: str, message: str, default: str = "") -> str:
        answer = self._prompt(Prompt(prompt_id, "text", message, default=default))
        value = answer.get("answer")
        if value is None or not isinstance(value, str):
            return default
        value = value.strip()
        return value if value else default

    def path(self, prompt_id: str, message: str, default: str = "") -> str:
        answer = self._prompt(Prompt(prompt_id, "path", message, default=default))
        value = answer.get("answer")
        if value is None or not isinstance(value, str):
            return default
        value = value.strip()
        return value if value else default

    def confirm(self, prompt_id: str, message: str, default: bool = False) -> bool:
        answer = self._prompt(Prompt(prompt_id, "confirm", message, default=default))
        value = answer.get("answer")
        if isinstance(value, bool):
            return value
        raise WizardProtocolError(f"confirm answer must be a boolean, got {type(value).__name__}")

    @staticmethod
    def _value_preview(value: str) -> str:
        """Render one value without allowing an error event to echo huge input."""
        if len(value) <= _CHOICE_VALUE_PREVIEW_LIMIT:
            return json.dumps(value, ensure_ascii=False)
        omitted = len(value) - _CHOICE_VALUE_PREVIEW_LIMIT
        prefix = json.dumps(value[:_CHOICE_VALUE_PREVIEW_LIMIT], ensure_ascii=False)
        return f"{prefix}... (+{omitted} chars)"

    @classmethod
    def _allowed_values(cls, values: list[str]) -> str:
        """Render a deterministic, bounded preview of offered values."""
        preview = ", ".join(cls._value_preview(value) for value in values[:_CHOICE_PREVIEW_LIMIT])
        remaining = len(values) - _CHOICE_PREVIEW_LIMIT
        if remaining > 0:
            preview = f"{preview}, " if preview else preview
            preview += f"... (+{remaining} more)"
        return f"[{preview}]"

    def select(self, prompt_id: str, message: str, choices: list[Choice], default: str | None = None) -> str:
        answer = self._prompt(Prompt(prompt_id, "select", message, choices=choices, default=default))
        value = answer.get("answer")
        allowed_values = [choice.value for choice in choices]
        allowed = self._allowed_values(allowed_values)
        if not isinstance(value, str):
            raise WizardProtocolError(
                f"select prompt {prompt_id!r}: answer must be a string, got {type(value).__name__}; "
                f"allowed values: {allowed}"
            )
        if value not in set(allowed_values):
            raise WizardProtocolError(
                f"select prompt {prompt_id!r}: answer value {self._value_preview(value)} "
                f"is not an offered choice value; allowed values: {allowed}"
            )
        return value

    def checkbox(self, prompt_id: str, message: str, choices: list[Choice]) -> list[str]:
        answer = self._prompt(Prompt(prompt_id, "checkbox", message, choices=choices))
        value = answer.get("answer")
        allowed_values = [choice.value for choice in choices]
        allowed_set = set(allowed_values)
        allowed = self._allowed_values(allowed_values)
        if not isinstance(value, list):
            raise WizardProtocolError(
                f"checkbox prompt {prompt_id!r}: answer must be a list, got {type(value).__name__}; "
                f"allowed values: {allowed}"
            )

        seen: set[str] = set()
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise WizardProtocolError(
                    f"checkbox prompt {prompt_id!r}: item at index {index} must be a string, "
                    f"got {type(item).__name__}; allowed values: {allowed}"
                )
            if item not in allowed_set:
                raise WizardProtocolError(
                    f"checkbox prompt {prompt_id!r}: item at index {index} value "
                    f"{self._value_preview(item)} is not an offered choice value; allowed values: {allowed}"
                )
            if item in seen:
                raise WizardProtocolError(
                    f"checkbox prompt {prompt_id!r}: duplicate value {self._value_preview(item)} "
                    f"at index {index}; allowed values: {allowed}"
                )
            seen.add(item)
        return list(value)

    def notice(self, message: str) -> None:
        self._emit(Event("notice", message=message).to_json())

    def progress(self, message: str) -> None:
        self._emit(Event("progress", message=message).to_json())

    def complete(self, message: str, data: dict | None = None) -> None:
        self._emit(Event("complete", message=message, data=data).to_json())

    def error(self, message: str) -> None:
        self._emit(Event("error", message=message).to_json())

    def cancelled(self) -> None:
        self._emit(Event("cancelled").to_json())
