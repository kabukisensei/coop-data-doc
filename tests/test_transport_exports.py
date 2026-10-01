import csv
import io
import json

from coop_data_doc.graph import Column, LineageGraph, Node, NodeType
from coop_data_doc.render.export import export_csvs
from coop_data_doc.wizard_io import JsonlWizardIO


def test_jsonl_reconfigures_non_utf8_pipes_bidirectionally():
    input_bytes = io.BytesIO(
        (json.dumps({"id": "name", "answer": "客户 é"}, ensure_ascii=False) + "\n").encode("utf-8")
    )
    output_bytes = io.BytesIO()
    stdin = io.TextIOWrapper(input_bytes, encoding="cp1252")
    stdout = io.TextIOWrapper(output_bytes, encoding="cp1252")
    wizard = JsonlWizardIO(stdin, stdout)
    assert wizard.text("name", "客户 name?") == "客户 é"
    stdout.flush()
    lines = output_bytes.getvalue().decode("utf-8").splitlines()
    assert json.loads(lines[-1])["message"] == "客户 name?"


def test_csv_exports_neutralize_formulas_in_all_user_fields(tmp_path):
    graph = LineageGraph()
    graph.add_node(
        Node(
            id="measure:model.m",
            node_type=NodeType.MEASURE,
            name="m",
            schema_name="@model",
            display_name="=HYPERLINK(1)",
            source_file="\tfile",
            columns=[Column(name="+column", data_type="-type")],
            metadata={"dax": " =1+1"},
        )
    )
    export_csvs(graph, tmp_path)
    for file in tmp_path.glob("*.csv"):
        rows = list(csv.reader(file.open(encoding="utf-8-sig")))
        for row in rows[1:]:
            assert all(not cell.lstrip().startswith(("=", "+", "-", "@")) for cell in row)
            assert all(not cell.startswith("\t") for cell in row)
    assert "'=HYPERLINK(1)" in (tmp_path / "objects.csv").read_text(encoding="utf-8-sig")
