"""Minimal Hyperliquid parquet lake layout helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


def _coin_dir(coin: str) -> str:
    return coin.strip().upper().replace("/", "-")


@dataclass(frozen=True, slots=True)
class LakeWrite:
    """Result of one parquet write."""

    path: Path
    rows: int


@dataclass(frozen=True, slots=True)
class HyperliquidLakeLayout:
    """File layout for Hyperliquid parquet artifacts."""

    root: Path

    def bronze_fills_path(self, coin: str, date: str) -> Path:
        symbol = _coin_dir(coin)
        return self.root / "bronze" / "hyperliquid" / "fills" / symbol / f"{symbol}-{date}.parquet"

    def silver_fills_path(self, coin: str, date: str) -> Path:
        symbol = _coin_dir(coin)
        return self.root / "silver" / "hyperliquid" / "fills" / symbol / f"{symbol}-{date}.parquet"

    def silver_l2_book_path(self, coin: str, date: str) -> Path:
        symbol = _coin_dir(coin)
        return (
            self.root
            / "silver"
            / "hyperliquid"
            / "l2_book"
            / symbol
            / f"{symbol}-{date}.parquet"
        )

    def silver_asset_ctxs_path(self, date: str) -> Path:
        return self.root / "silver" / "hyperliquid" / "asset_ctxs" / f"asset-ctxs-{date}.parquet"

    def silver_funding_path(self, coin: str, date: str) -> Path:
        symbol = _coin_dir(coin)
        return (
            self.root
            / "silver"
            / "hyperliquid"
            / "funding"
            / symbol
            / f"{symbol}-funding-{date}.parquet"
        )

    def silver_candles_path(self, coin: str, interval: str, date: str) -> Path:
        symbol = _coin_dir(coin)
        return (
            self.root
            / "silver"
            / "hyperliquid"
            / "candles"
            / interval
            / symbol
            / f"{symbol}-{interval}-{date}.parquet"
        )

    def gold_markout_path(self, coin: str, date: str) -> Path:
        symbol = _coin_dir(coin)
        return (
            self.root
            / "gold"
            / "hyperliquid"
            / "markout"
            / symbol
            / f"{symbol}-markout-{date}.parquet"
        )

    def gold_training_path(self, coin: str, date: str) -> Path:
        symbol = _coin_dir(coin)
        return (
            self.root
            / "gold"
            / "hyperliquid"
            / "training"
            / symbol
            / f"{symbol}-training-{date}.parquet"
        )


def write_parquet_records(path: Path, records: Sequence[Mapping[str, Any]]) -> LakeWrite:
    """Write non-empty records to a zstd-compressed parquet file."""
    if not records:
        raise ValueError("cannot write an empty Hyperliquid parquet artifact")
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist([dict(record) for record in records])
    _write_table(path, table)
    return LakeWrite(path=path, rows=len(records))


def read_parquet_records(path: Path) -> list[dict[str, Any]]:
    """Read a parquet artifact back into dictionaries."""
    table = _read_table(path)
    return [dict(record) for record in table.to_pylist()]


def _write_table(path: Path, table: pa.Table) -> None:
    pq.write_table(table, path, compression="zstd")  # type: ignore[no-untyped-call]


def _read_table(path: Path) -> pa.Table:
    return pq.read_table(path)  # type: ignore[no-untyped-call]
