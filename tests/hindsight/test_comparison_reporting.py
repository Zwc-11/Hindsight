from __future__ import annotations

import builtins
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hindsight.cli import default_exec_config, default_naive_config, main, run_comparison
from hindsight.core.events import KlineEvent
from hindsight.execution.compare import compare_naive_vs_realistic, sharpe_ratio
from hindsight.reporting.backtest_report import (
    comparison_payload,
    write_comparison_json,
    write_comparison_markdown,
)
from hindsight.reporting.curves import ascii_sparkline, write_curve_png
from hindsight.strategy.baselines.momentum import MomentumStrategy

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def kline(index: int, close: float) -> KlineEvent:
    timestamp = NOW + timedelta(minutes=index)
    return KlineEvent(
        symbol="BTCUSDT",
        timestamp=timestamp,
        sequence=index,
        interval="1m",
        open_time=timestamp,
        close_time=timestamp,
        open_price=close,
        high_price=close + 1,
        low_price=close - 1,
        close_price=close,
        volume=1,
        trade_count=1,
    )


def events() -> list[KlineEvent]:
    return [
        kline(0, 100),
        kline(1, 102),
        kline(2, 99),
        kline(3, 103),
        kline(4, 98),
        kline(5, 104),
    ]


def test_compare_naive_vs_realistic_produces_verdict_and_reports(tmp_path: Path) -> None:
    result = compare_naive_vs_realistic(
        events=events(),
        strategy_factory=lambda: MomentumStrategy(
            symbol="BTCUSDT",
            quantity=1,
            lookback_bars=1,
            threshold_bps=1,
        ),
        symbol="BTCUSDT",
        naive_config=default_naive_config(),
        realistic_config=default_exec_config(),
    )
    assert result.naive.orders_emitted > 0
    assert result.realistic.final_state.fees_paid > 0
    assert "Realistic execution" in result.verdict
    payload = comparison_payload(result)
    assert payload["verdict"] == result.verdict
    json_path = tmp_path / "comparison.json"
    markdown_path = tmp_path / "comparison.md"
    write_comparison_json(json_path, result)
    write_comparison_markdown(markdown_path, result)
    assert "final_equity" in json_path.read_text(encoding="utf-8")
    assert markdown_path.read_text(encoding="utf-8").startswith("# Hindsight")


def test_sharpe_ratio_rejects_undefined_inputs() -> None:
    with pytest.raises(ValueError, match="at least three"):
        sharpe_ratio([1, 2])
    with pytest.raises(ValueError, match="zero-variance"):
        sharpe_ratio([1, 1, 1])


def test_ascii_sparkline_and_png_optional_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert ascii_sparkline([1, 2, 3])
    assert ascii_sparkline([1, 1]) == "  "
    with pytest.raises(ValueError, match="sparkline"):
        ascii_sparkline([])

    real_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "matplotlib":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    result = compare_naive_vs_realistic(
        events=events(),
        strategy_factory=lambda: MomentumStrategy("BTCUSDT", 1, 1, 1),
        symbol="BTCUSDT",
        naive_config=default_naive_config(),
        realistic_config=default_exec_config(),
    )
    assert write_curve_png(tmp_path / "curve.png", result.realistic.equity_curve) is False


def test_write_curve_png_success_with_fake_matplotlib(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    fake_pyplot = types.SimpleNamespace(
        figure=lambda *args, **kwargs: calls.append("figure"),
        plot=lambda *args, **kwargs: calls.append("plot"),
        tight_layout=lambda: calls.append("tight_layout"),
        savefig=lambda path: calls.append(str(path)),
        close=lambda: calls.append("close"),
    )
    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "matplotlib":
            return types.SimpleNamespace(pyplot=fake_pyplot)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = compare_naive_vs_realistic(
        events=events(),
        strategy_factory=lambda: MomentumStrategy("BTCUSDT", 1, 1, 1),
        symbol="BTCUSDT",
        naive_config=default_naive_config(),
        realistic_config=default_exec_config(),
    )
    assert write_curve_png(tmp_path / "curve.png", result.realistic.equity_curve) is True
    assert "plot" in calls


def test_compare_cli_writes_outputs_on_tiny_lake(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    lake = tmp_path / "lake"
    out = tmp_path / "out"
    path = lake / "klines" / "BTCUSDT" / "1m" / "BTCUSDT-klines-1m-2026-01-01.parquet"
    path.parent.mkdir(parents=True)
    table = pa.table(
        {
            "event_id": [f"k{i}" for i in range(6)],
            "timestamp": [
                (NOW + timedelta(minutes=i)).isoformat().replace("+00:00", "Z")
                for i in range(6)
            ],
            "open_price": [100.0, 102.0, 99.0, 103.0, 98.0, 104.0],
            "high_price": [101.0, 103.0, 100.0, 104.0, 99.0, 105.0],
            "low_price": [99.0, 101.0, 98.0, 102.0, 97.0, 103.0],
            "close_price": [100.0, 102.0, 99.0, 103.0, 98.0, 104.0],
            "volume": [1.0] * 6,
            "trade_count": [1] * 6,
        }
    )
    pq.write_table(table, path)
    artifacts = run_comparison(
        lake_root=lake,
        output_dir=out,
        symbol="BTCUSDT",
        date="2026-01-01",
        limit=6,
        quantity=1,
        lookback_bars=1,
        threshold_bps=1,
    )
    assert artifacts.json_path.exists()
    assert artifacts.markdown_path.exists()
    assert main(
        [
            "compare",
            "--lake-root",
            str(lake),
            "--output-dir",
            str(out),
            "--date",
            "2026-01-01",
            "--limit",
            "6",
        ]
    ) == 0


def test_compare_cli_handles_missing_optional_png(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    lake = tmp_path / "lake"
    out = tmp_path / "out"
    path = lake / "klines" / "BTCUSDT" / "1m" / "BTCUSDT-klines-1m-2026-01-01.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "event_id": [f"k{i}" for i in range(6)],
                "timestamp": [
                    (NOW + timedelta(minutes=i)).isoformat().replace("+00:00", "Z")
                    for i in range(6)
                ],
                "open_price": [100.0, 102.0, 99.0, 103.0, 98.0, 104.0],
                "high_price": [101.0, 103.0, 100.0, 104.0, 99.0, 105.0],
                "low_price": [99.0, 101.0, 98.0, 102.0, 97.0, 103.0],
                "close_price": [100.0, 102.0, 99.0, 103.0, 98.0, 104.0],
                "volume": [1.0] * 6,
                "trade_count": [1] * 6,
            }
        ),
        path,
    )
    real_import = builtins.__import__

    def blocked_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "matplotlib":
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    assert main(
        [
            "compare",
            "--lake-root",
            str(lake),
            "--output-dir",
            str(out),
            "--date",
            "2026-01-01",
            "--limit",
            "6",
        ]
    ) == 0


def test_compare_fails_loudly_when_no_market_events_load(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="No market events loaded"):
        run_comparison(
            lake_root=tmp_path / "missing",
            output_dir=tmp_path,
            symbol="BTCUSDT",
            date="2026-01-01",
            limit=10,
            quantity=1,
            lookback_bars=1,
            threshold_bps=1,
        )
