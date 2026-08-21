"""Parity tests: terminal (questionary) and JSONL transports must produce the
same prompts and the same resulting config for equivalent answers."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from coop_data_doc import wizard_io
from coop_data_doc.wizard import run_setup
from coop_data_doc.wizard_io import Choice, JsonlWizardIO, WizardIO


class ScriptedWizardIO(WizardIO):
    """Answers by matching a prompt's message against a {substring: answer} map.

    Records every prompt (kind, message, default, choices) for parity assertions.
    Unmatched text keeps the default; unmatched confirm -> False; unmatched
    checkbox keeps everything checked; unmatched select -> first choice.
    """

    def __init__(self, answers: dict[str, Any]):
        self.answers = answers
        self.prompts: list[dict] = []

    def _match(self, message: str, kind: str, default: Any, choices: list[Choice]) -> Any:
        for key, value in self.answers.items():
            if key.lower() in message.lower():
                return value
        if kind == "confirm":
            return False
        if kind == "checkbox":
            return [c.value for c in choices]
        if kind == "select":
            return choices[0].value if choices else ""
        return default if default is not None else ""

    def _record(self, kind: str, message: str, default: Any, choices: list[Choice]) -> None:
        self.prompts.append(
            {
                "kind": kind,
                "message": message,
                "default": default,
                "choices": [(c.label, c.value, c.checked) for c in choices],
            }
        )

    def text(self, prompt_id: str, message: str, default: str = "") -> str:
        self._record("text", message, default, [])
        return str(self._match(message, "text", default, []))

    def path(self, prompt_id: str, message: str, default: str = "") -> str:
        self._record("path", message, default, [])
        return str(self._match(message, "path", default, []))

    def confirm(self, prompt_id: str, message: str, default: bool = False) -> bool:
        self._record("confirm", message, default, [])
        return bool(self._match(message, "confirm", default, []))

    def select(self, prompt_id: str, message: str, choices: list[Choice], default: str | None = None) -> str:
        self._record("select", message, default, choices)
        return str(self._match(message, "select", default, choices))

    def checkbox(self, prompt_id: str, message: str, choices: list[Choice]) -> list[str]:
        self._record("checkbox", message, default=None, choices=choices)
        return [str(v) for v in self._match(message, "checkbox", None, choices)]

    def notice(self, message: str) -> None:
        pass


def _make_repos(tmp_path: Path) -> None:
    (tmp_path / "sql-repo").mkdir()
    (tmp_path / "pbi-repo").mkdir()


def _answers() -> dict[str, Any]:
    return {
        "Project name": "Parity Estate",
        "SQL repo path": "./sql-repo",
        "Power BI repo path": "./pbi-repo",
        "Markdown output": "./data-docs",
        "HTML site": "./data-docs-site",
        "SQL — files/patterns to INCLUDE": "**/*.sql",
        "Bronze layer — schemas": "erp_orders, erp_finance",
        "Silver layer — schemas": "stg",
        "Gold layer — schemas": "mart, common, silver",
    }


def test_jsonl_and_terminal_produce_same_config(tmp_path: Path):
    _make_repos(tmp_path)
    answers = _answers()

    # Terminal path — monkeypatch the questionary module used by QuestionaryWizardIO.
    calls: list[tuple[str, str, dict]] = []
    router = _questionary_router(answers, calls)
    fake = _RoutedQuestionary(router)
    monkeypatch_q = pytest.MonkeyPatch()
    monkeypatch_q.setattr(wizard_io, "questionary", fake)
    try:
        terminal_config = run_setup(tmp_path / "coop-data-doc-terminal.yml")
    finally:
        monkeypatch_q.undo()
    assert terminal_config is not None

    # JSONL path — same answers through the transport abstraction.
    scripted = ScriptedWizardIO(answers)
    jsonl_config = run_setup(tmp_path / "coop-data-doc-jsonl.yml", io=scripted)
    assert jsonl_config is not None

    # The two configs agree on the fields both flows set.
    assert terminal_config.project_name == jsonl_config.project_name == "Parity Estate"
    assert terminal_config.repos["sql"].path == jsonl_config.repos["sql"].path == "./sql-repo"
    assert terminal_config.repos["powerbi"].path == jsonl_config.repos["powerbi"].path == "./pbi-repo"
    assert terminal_config.layers == jsonl_config.layers

    # Prompt text parity: the JSONL transport emitted the same messages as the
    # terminal flow (compare the recorded questionary calls' messages to the
    # ScriptedWizardIO's recorded prompt messages).
    q_messages = [m for _, m, _ in calls]
    io_messages = [p["message"] for p in scripted.prompts]
    assert io_messages == q_messages


def test_jsonl_transport_emits_prompt_json(tmp_path: Path):
    """The real JsonlWizardIO emits the spec'd JSON prompt shape on stdout."""
    _make_repos(tmp_path)
    stdin = io.StringIO(
        json.dumps({"id": "project_name", "answer": "X"})
        + "\n"
        + json.dumps({"id": "sql_repo_path_path", "answer": "./sql-repo"})
        + "\n"
        + json.dumps({"id": "pbi_repo_path_path", "answer": "./pbi-repo"})
        + "\n"
    )
    stdout = io.StringIO()
    io_obj = JsonlWizardIO(stdin, stdout)

    io_obj.text("project_name", "Project name (shown as the docs site title):", "Coop BI Estate")
    io_obj.path(
        "sql_repo_path_path", "SQL repo path — the folder with your procs, tables, views:", "../sql-repo"
    )
    io_obj.path(
        "pbi_repo_path_path",
        "Power BI repo path — the folder with your semantic models and reports:",
        "../pbi-repo",
    )

    stdout.seek(0)
    emitted = [json.loads(line) for line in stdout if line.strip()]
    assert emitted[0] == {
        "type": "prompt",
        "id": "project_name",
        "kind": "text",
        "message": "Project name (shown as the docs site title):",
        "default": "Coop BI Estate",
        "choices": [],
    }
    assert emitted[1]["kind"] == "path"
    assert emitted[1]["id"] == "sql_repo_path_path"


def test_cli_jsonl_transport_emits_only_json(tmp_path: Path, monkeypatch):
    """The CLI's `--transport jsonl` path must keep stdout line-delimited JSON —
    the success summary and 'Setup complete' go out as notice events, never raw
    text that would break a bridge parser (regression guard)."""
    import os

    from click.testing import CliRunner

    from coop_data_doc import cli as cli_module
    from coop_data_doc import wizard as wizard_module
    from coop_data_doc.config import Config

    (tmp_path / "coop-data-doc.yml").write_text("project_name: J\nrepos: {}\n", encoding="utf-8")

    def fake_run_setup(path, io=None):
        assert io is not None  # jsonl transport must hand run_setup a WizardIO
        io.text("q1", "Project name?", "J")
        return Config.load(Path(path))

    monkeypatch.setattr(wizard_module, "run_setup", fake_run_setup)

    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = CliRunner().invoke(
            cli_module.cli,
            ["setup", "--transport", "jsonl"],
            input='{"id":"q1","answer":"J"}\n',
        )
    finally:
        os.chdir(old)

    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    events = [json.loads(line) for line in lines]  # every line must be valid JSON
    assert events[0]["type"] == "prompt"
    notices = [e.get("message", "") for e in events if e["type"] == "notice"]
    assert any(m.startswith("Saved ") for m in notices)
    assert any("Setup complete." in m for m in notices)


# --- minimal questionary stand-in (mirrors tests/test_wizard.py) -------------


def _questionary_router(answers: dict[str, Any], calls: list):
    def router(kind, message, kwargs):
        calls.append((kind, message, kwargs))
        for key, value in answers.items():
            if key.lower() in message.lower():
                return value
        if kind == "confirm":
            return False
        if kind == "checkbox":
            return [choice.value for choice in kwargs.get("choices", [])]
        return kwargs.get("default", "")

    return router


class _RoutedQuestionary:
    import questionary as _questionary

    Choice = _questionary.Choice
    Separator = _questionary.Separator

    def __init__(self, router):
        self.router = router

    def _q(self, kind, message, **kwargs):
        answer = self.router(kind, message, kwargs)

        class _Result:
            @staticmethod
            def ask():
                return answer

            @staticmethod
            def unsafe_ask():
                return answer

        return _Result()

    def text(self, message, **kwargs):
        return self._q("text", message, **kwargs)

    def path(self, message, **kwargs):
        return self._q("path", message, **kwargs)

    def confirm(self, message, **kwargs):
        return self._q("confirm", message, **kwargs)

    def checkbox(self, message, **kwargs):
        return self._q("checkbox", message, **kwargs)
