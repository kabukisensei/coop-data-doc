"""Tests for WizardIO protocol and JSONL transport."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from coop_data_doc.wizard_io import MAX_JSONL_LINE, Choice, JsonlWizardIO, WizardProtocolError


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
    hello = json.loads(stdout.readline())
    assert hello == {"type": "hello", "protocol_version": JsonlWizardIO.PROTOCOL_VERSION}
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


@pytest.mark.parametrize("line", ["not-json\n", "[]\n", '{"id":"wrong","answer":"x"}\n'])
def test_jsonl_invalid_answer_is_protocol_error(line):
    io_obj = JsonlWizardIO(io.StringIO(line), io.StringIO())
    with pytest.raises(WizardProtocolError):
        io_obj.text("expected", "Project name:")


def test_jsonl_accepts_crlf_and_preserves_unicode_separators():
    value = "A\u2028B\u2029C"
    line = json.dumps({"id": "q", "answer": value}, ensure_ascii=False) + "\r\n"
    assert JsonlWizardIO(io.StringIO(line), io.StringIO()).text("q", "Name") == value


def test_jsonl_cancel_answer_raises_keyboard_interrupt():
    line = json.dumps({"id": "q", "cancelled": True}) + "\n"
    with pytest.raises(KeyboardInterrupt):
        JsonlWizardIO(io.StringIO(line), io.StringIO()).text("q", "Name")


def test_jsonl_terminal_events_have_explicit_shapes():
    stdout = io.StringIO()
    io_obj = JsonlWizardIO(io.StringIO(), stdout)
    io_obj.progress("Scanning")
    io_obj.complete("Done", {"config": "x.yml"})
    events = [json.loads(line) for line in stdout.getvalue().splitlines()]
    # The constructor emits hello first; progress/complete follow.
    assert events[0] == {"type": "hello", "protocol_version": JsonlWizardIO.PROTOCOL_VERSION}
    assert events[1:] == [
        {"type": "progress", "message": "Scanning"},
        {"type": "complete", "message": "Done", "data": {"config": "x.yml"}},
    ]


def test_jsonl_answer_type_validation():
    """confirm/select/checkbox answers must have the documented types (1.1.1)."""
    # confirm: non-boolean answer is a protocol error, not a silent default.
    io_obj = JsonlWizardIO(io.StringIO(_answer("c1", "banana")), io.StringIO())
    with pytest.raises(WizardProtocolError, match="must be a boolean"):
        io_obj.confirm("c1", "Proceed?")
    io_obj = JsonlWizardIO(io.StringIO(_answer("c2", True)), io.StringIO())
    assert io_obj.confirm("c2", "Proceed?", default=False) is True

    # select: non-string answer is a protocol error.
    io_obj = JsonlWizardIO(io.StringIO(_answer("s1", 3)), io.StringIO())
    with pytest.raises(WizardProtocolError, match="must be a string"):
        io_obj.select("s1", "Pick:", [Choice("a", "A")])

    # checkbox: non-list answer is a protocol error.
    io_obj = JsonlWizardIO(io.StringIO(_answer("cb1", "a")), io.StringIO())
    with pytest.raises(WizardProtocolError, match="must be a list"):
        io_obj.checkbox("cb1", "Pick:", [Choice("a", "A")])


def test_jsonl_select_rejects_unknown_value():
    choices = [Choice("Schema A", "schema_a"), Choice("Schema B", "schema_b")]
    io_obj = JsonlWizardIO(io.StringIO(_answer("select_unknown", "missing")), io.StringIO())

    with pytest.raises(WizardProtocolError) as exc_info:
        io_obj.select("select_unknown", "Pick schema:", choices)

    message = str(exc_info.value)
    assert "select_unknown" in message
    assert "allowed values" in message
    assert '"schema_a"' in message and '"schema_b"' in message


@pytest.mark.parametrize("answer", ["Schema A", "SCHEMA_A"], ids=["label", "case-mismatch"])
def test_jsonl_select_rejects_label_and_case_mismatch(answer: str):
    choices = [Choice("Schema A", "schema_a"), Choice("Schema B", "schema_b")]
    io_obj = JsonlWizardIO(io.StringIO(_answer("select_exact", answer)), io.StringIO())

    with pytest.raises(WizardProtocolError, match="select_exact"):
        io_obj.select("select_exact", "Pick schema:", choices)


@pytest.mark.parametrize(
    "answer", [["missing"], [1], [True], [None]], ids=["unknown", "integer", "boolean", "null"]
)
def test_jsonl_checkbox_rejects_invalid_item_values(answer):
    choices = [Choice("A", "a"), Choice("B", "b")]
    io_obj = JsonlWizardIO(io.StringIO(_answer("checkbox_item", answer)), io.StringIO())

    with pytest.raises(WizardProtocolError) as exc_info:
        io_obj.checkbox("checkbox_item", "Pick:", choices)

    message = str(exc_info.value)
    assert "checkbox_item" in message
    assert "allowed values" in message
    assert '"a"' in message and '"b"' in message


def test_jsonl_checkbox_rejects_mixed_valid_and_invalid_values():
    choices = [Choice("A", "a"), Choice("B", "b")]
    io_obj = JsonlWizardIO(io.StringIO(_answer("checkbox_mixed", ["a", "missing", "b"])), io.StringIO())

    with pytest.raises(WizardProtocolError, match="checkbox_mixed"):
        io_obj.checkbox("checkbox_mixed", "Pick:", choices)


def test_jsonl_checkbox_rejects_duplicate_values():
    choices = [Choice("A", "a"), Choice("B", "b")]
    io_obj = JsonlWizardIO(io.StringIO(_answer("checkbox_duplicate", ["a", "a"])), io.StringIO())

    with pytest.raises(WizardProtocolError, match="checkbox_duplicate"):
        io_obj.checkbox("checkbox_duplicate", "Pick:", choices)


def test_jsonl_checkbox_empty_list_is_valid():
    choices = [Choice("A", "a"), Choice("B", "b")]
    io_obj = JsonlWizardIO(io.StringIO(_answer("checkbox_empty", [])), io.StringIO())

    assert io_obj.checkbox("checkbox_empty", "Pick:", choices) == []


def test_jsonl_checkbox_preserves_answer_order():
    choices = [Choice("A", "a"), Choice("B", "b"), Choice("C", "c")]
    io_obj = JsonlWizardIO(io.StringIO(_answer("checkbox_order", ["c", "a", "b"])), io.StringIO())

    assert io_obj.checkbox("checkbox_order", "Pick:", choices) == ["c", "a", "b"]


def test_jsonl_unicode_choice_values_are_preserved():
    choices = [Choice("Cafe", "café.表"), Choice("Path", "путь")]
    select_io = JsonlWizardIO(io.StringIO(_answer("unicode_select", "café.表")), io.StringIO())
    checkbox_io = JsonlWizardIO(io.StringIO(_answer("unicode_checkbox", ["путь", "café.表"])), io.StringIO())

    assert select_io.select("unicode_select", "Pick:", choices) == "café.表"
    assert checkbox_io.checkbox("unicode_checkbox", "Pick:", choices) == ["путь", "café.表"]


def test_jsonl_choice_error_preview_is_bounded_and_deterministic():
    choices = [Choice(f"Choice {index}", f"value-{index}") for index in range(12)]
    io_obj = JsonlWizardIO(io.StringIO(_answer("many_choices", "unknown")), io.StringIO())

    with pytest.raises(WizardProtocolError) as exc_info:
        io_obj.select("many_choices", "Pick:", choices)

    message = str(exc_info.value)
    assert "many_choices" in message
    assert '"value-0"' in message and '"value-7"' in message
    assert '"value-8"' not in message
    assert "... (+4 more)" in message


def test_jsonl_choice_error_truncates_long_submitted_and_allowed_values():
    long_allowed = "a" * 500
    long_answer = "z" * 500
    choices = [Choice("Long", long_allowed)]
    io_obj = JsonlWizardIO(io.StringIO(_answer("long_choice", long_answer)), io.StringIO())

    with pytest.raises(WizardProtocolError) as exc_info:
        io_obj.select("long_choice", "Pick:", choices)

    message = str(exc_info.value)
    assert len(message) < 400
    assert "z" * 80 in message and "z" * 81 not in message
    assert "a" * 80 in message and "a" * 81 not in message
    assert "(+420 chars)" in message


def test_jsonl_wrong_id_cancel_is_error_not_cancel():
    """id is validated before cancelled — a mismatched cancel is a protocol error."""
    io_obj = JsonlWizardIO(io.StringIO('{"id":"wrong","cancelled":true}\n'), io.StringIO())
    with pytest.raises(WizardProtocolError, match="id mismatch"):
        io_obj.confirm("right", "Proceed?")


def test_jsonl_max_line_is_content_only():
    """A content line of exactly MAX_JSONL_LINE chars is accepted; over is rejected."""
    # Content (after LF strip) exactly at the limit → accepted.
    exact = '{"id":"q1","answer":"' + "x" * (MAX_JSONL_LINE - 23) + '"}'
    assert len(exact) == MAX_JSONL_LINE
    io_obj = JsonlWizardIO(io.StringIO(exact + "\n"), io.StringIO())
    assert io_obj.text("q1", "Big:") == "x" * (MAX_JSONL_LINE - 23)

    oversize = '{"id":"q1","answer":"' + "x" * MAX_JSONL_LINE + '"}'
    io_obj = JsonlWizardIO(io.StringIO(oversize + "\n"), io.StringIO())
    with pytest.raises(WizardProtocolError, match="exceeds 1 MiB"):
        io_obj.text("q1", "Bigger:")
