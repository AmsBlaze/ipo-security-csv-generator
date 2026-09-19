import csv
import importlib.util
import pathlib
import json

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generator", ROOT / "scripts" / "generate_csv.py"
)
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


def test_generation_changes_only_four_fields(tmp_path, monkeypatch):
    mock = generator.fetch_mock(ROOT / "tests" / "mock_upstox.json")

    original_template = generator.TEMPLATE
    original_output = generator.OUTPUT
    original_metadata = generator.METADATA

    template_copy = tmp_path / "template.csv"
    template_copy.write_bytes(original_template.read_bytes())
    monkeypatch.setattr(generator, "TEMPLATE", template_copy)
    monkeypatch.setattr(generator, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(generator, "OUTPUT", tmp_path / "output.csv")
    monkeypatch.setattr(generator, "METADATA", tmp_path / "latest.json")

    count, _ = generator.generate(mock)
    assert count == 3

    with template_copy.open("r", encoding="utf-8-sig", newline="") as f:
        template_rows = list(csv.reader(f))

    with (tmp_path / "output.csv").open("r", encoding="utf-8", newline="") as f:
        output_rows = list(csv.reader(f))

    header = output_rows[0]
    template = template_rows[1]
    changed = {"FinInstrmId", "TckrSymb", "FinInstrmNm", "ISIN"}

    assert len(output_rows) == 4

    ids = set()
    for row in output_rows[1:]:
        assert len(row) == len(header)
        ids.add(row[header.index("FinInstrmId")])

        for i, col in enumerate(header):
            if col not in changed:
                assert row[i] == template[i]

    assert len(ids) == 3
    assert all(len(x) == 6 and x.isdigit() for x in ids)
