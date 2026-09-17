from __future__ import annotations

import json

import pytest

from hindsight import economic_store

pytest.importorskip("duckdb")


def report_fixture(tmp_path, *, empty=False):
    decision = dict(
        at=1,
        prediction=None,
        target_shares=0,
        training_count=0,
        trained_through=None,
        model_hash=None,
        features=None,
    )
    event = dict(
        order_id=1,
        at=1,
        phase="no_order",
        shares=0,
        price_micros=0,
        fee_micros=0,
        cash_micros=100000000,
        position=0,
        reason="warmup",
    )
    report = dict(
        schema_version=1,
        input_hash="a" * 64,
        synthetic=True,
        trials=[
            dict(
                variant="A",
                decisions=[] if empty else [decision],
                scores=[],
                account=dict(events=[] if empty else [event]),
            )
        ],
    )
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    return path


def test_export_preserves_integer_money_and_nulls(tmp_path):
    import duckdb

    path = report_fixture(tmp_path)
    counts = economic_store.export_warehouse(path, tmp_path / "warehouse")
    assert counts == dict(decisions=1, scores=0, events=1)
    with duckdb.connect(str(tmp_path / "warehouse/research.duckdb"), read_only=True) as db:
        assert db.execute("SELECT cash_micros FROM events").fetchone() == (100000000,)
        assert db.execute("SELECT prediction FROM decisions").fetchone() == (None,)
        assert db.execute("SELECT COUNT(*) FROM provenance").fetchone() == (4,)
    assert (tmp_path / "warehouse/_SUCCESS.json").is_file()


def test_empty_tables_have_stable_schema(tmp_path):
    counts = economic_store.export_warehouse(
        report_fixture(tmp_path, empty=True), tmp_path / "empty"
    )
    assert counts == dict(decisions=0, scores=0, events=0)


def test_warehouse_refuses_overwrite(tmp_path):
    path = report_fixture(tmp_path)
    output = tmp_path / "exists"
    output.mkdir()
    with pytest.raises(FileExistsError):
        economic_store.export_warehouse(path, output)


def test_warehouse_rejects_wrong_schema(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"schema_version": 9}')
    with pytest.raises(ValueError, match="schema-v1"):
        economic_store.export_warehouse(path, tmp_path / "out")


def test_missing_extra_is_actionable(tmp_path, monkeypatch):
    def missing(name):
        raise ImportError("not installed")

    monkeypatch.setattr(economic_store, "import_module", missing)
    with pytest.raises(RuntimeError, match="economic"):
        economic_store.export_warehouse(report_fixture(tmp_path), tmp_path / "out")


def test_warehouse_cli(tmp_path, capsys):
    path = report_fixture(tmp_path)
    assert economic_store.main([str(path), str(tmp_path / "warehouse")]) == 0
    assert '"decisions": 1' in capsys.readouterr().out
