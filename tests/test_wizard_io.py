"""Tests for WizardIO protocol and JSONL transport."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from coop_data_doc.wizard_io import Choice, JsonlWizardIO


def _answer(prompt_id: str, answer: Any) -> str:
    return json.dumps({"id": prompt_id, "answer": answer}, ensure_ascii=False) + "\n"


def test_jsonl_text_uses_default_when_answer_blank():
    stdin = io.StringIO(_answer("q1", ""))
    stdout = io.StringIO()
    io_obj = JsonlWizardIO(stdin, stdout)
    result = io_obj.text("q1", "Project name:", default="Coop BI Estate")
    assert result == "Coop BI Estate"


def test_jsonl_text_returns_answer():
    stdin = io.StringIO(_answer("q2", "Sales Warehouse"))
    stdout = io.StringIO()
    io_obj = JsonlWizardIO(stdin, stdout)
    result = io_obj.text("q2", "Project name:", default="Coop BI Estate")
    assert result == "Sales Warehouse"


def test_jsonl_confirm_true():
    stdin = io.StringIO(_answer("q3", True))
    stdout = io.StringIO()
    io_obj = JsonlWizardIO(stdin, stdout)
    assert io_obj.confirm("q3", "Build now?", default=False) is True


def test_jsonl_select_returns_value():
    stdin = io.StringIO(_answer("q4", "schema_a"))
    stdout = io.StringIO()
    io_obj = JsonlWizardIO(stdin, stdout)
    choices = [Choice("Schema A", "schema_a"), Choice("Schema B", "schema_b")]
    result = io_obj.select("q4", "Pick schema:", choices, default="schema_b")
    assert result == "schema_a"


def test_jsonl_checkbox_returns_values():
    stdin = io.StringIO(_answer("q5", ["a", "b"]))
    stdout = io.StringIO()
    io_obj = JsonlWizardIO(stdin, stdout)
    choices = [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")]
    result = io_obj.checkbox("q5", "Pick:", choices)
    assert result == ["a", "b"]


def test_jsonl_prompt_shape():
    stdin = io.StringIO(_answer("q6", "ok"))
    stdout = io.StringIO()
    io_obj = JsonlWizardIO(stdin, stdout)
    io_obj.text("q6", "Say ok:")
    stdout.seek(0)
    emitted = json.loads(stdout.readline())
    assert emitted == {
        "type": "prompt",
        "id": "q6",
        "kind": "text",
        "message": "Say ok:",
        "default": "",
        "choices": [],
    }


def test_jsonl_eof_raises_keyboard_interrupt():
    stdin = io.StringIO("")
    stdout = io.StringIO()
    io_obj = JsonlWizardIO(stdin, stdout)
    with pytest.raises(KeyboardInterrupt):
        io_obj.text("q7", "Project name:")
