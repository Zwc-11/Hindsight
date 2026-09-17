"""Measured local API workloads; no synthetic trading or claimed cold OS-cache result."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import platform
import statistics
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8780")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server-pid", type=int, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)

    def fetch(path: str) -> tuple[object, float, int]:
        started = time.perf_counter()
        with urllib.request.urlopen(args.url + path, timeout=30) as response:
            raw = response.read()
        return json.loads(raw), (time.perf_counter() - started) * 1000, len(raw)

    status, _, _ = fetch("/api/status")
    boot, _, _ = fetch("/api/bootstrap")
    query = {
        "snapshot": boot["snapshot"],
        "as_of": boot["now"],
        "mode": "observed",
        "purpose": "display",
    }
    catalog, _, _ = fetch(
        "/api/catalog?"
        + urllib.parse.urlencode(
            {
                **query,
                "provider": "worldbank",
                "search": "Exports of goods and services",
                "limit": 200,
            }
        )
    )
    series = [r["id"] for r in catalog["rows"] if r.get("captured_version_count", 0) >= 30]
    if len(series) < 8:
        raise RuntimeError("At least eight real series with captured history are needed")
    series = series[:24]
    first, _, _ = fetch(
        "/api/series/"
        + urllib.parse.quote(series[0], safe="")
        + "?"
        + urllib.parse.urlencode(query)
    )
    evidence = first["points"][0]["id"]
    workloads = {
        "catalog-50-rows": lambda i: (
            "/api/catalog?"
            + urllib.parse.urlencode({**query, "provider": "worldbank", "limit": 50})
        ),
        "annual-chart-bounded-2000": lambda i: (
            "/api/series/"
            + urllib.parse.quote(series[i % len(series)], safe="")
            + "?"
            + urllib.parse.urlencode({**query, "limit": 2000})
        ),
        "evidence-single-record": lambda i: (
            "/api/evidence/" + evidence + "?" + urllib.parse.urlencode(query)
        ),
        "disjoint-series-and-time-ranges": lambda i: (
            "/api/series/"
            + urllib.parse.quote(series[i % len(series)], safe="")
            + "?"
            + urllib.parse.urlencode(
                {
                    **query,
                    "start": f"{1960 + (i % 5) * 10}-01-01",
                    "end": f"{1969 + (i % 5) * 10}-12-31",
                    "limit": 2000,
                }
            )
        ),
    }
    results = []

    def resource_sample() -> dict[str, object]:
        raw = subprocess.check_output(
            ["ps", "-p", str(args.server_pid), "-o", "rss=,%cpu="], text=True
        ).strip()
        fields = raw.split()
        return {"rss_kib": int(fields[0]), "cpu_percent": float(fields[1])}

    resources = [resource_sample()]
    for name, path in workloads.items():
        for clients in (1, 8):
            samples = []
            errors = []

            def one(index: int, path=path, errors=errors) -> tuple[float, int] | None:
                try:
                    _, elapsed, size = fetch(path(index))
                    return elapsed, size
                except Exception as exc:
                    errors.append(str(exc))
                    return None

            started = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as pool:
                samples = [s for s in pool.map(one, range(120)) if s is not None]
            times = sorted(s[0] for s in samples)

            def quantile(q: float, values=times) -> float | None:
                return (
                    values[min(len(values) - 1, round((len(values) - 1) * q))] if values else None
                )

            results.append(
                {
                    "workload": name,
                    "concurrent_clients": clients,
                    "requests": 120,
                    "successful_requests": len(samples),
                    "p50_ms": quantile(0.5),
                    "p95_ms": quantile(0.95),
                    "p99_ms": quantile(0.99),
                    "mean_bytes": statistics.mean(s[1] for s in samples) if samples else None,
                    "wall_seconds": time.perf_counter() - started,
                    "errors": errors,
                }
            )
            resources.append(resource_sample())
    latest, _, _ = fetch("/api/status")
    report = {
        "real_data": True,
        "measurement": "HTTP client elapsed time on loopback; includes serialization and transport",
        "cache": (
            "No application result cache. OS page cache not forcibly purged; no cold-cache claim."
        ),
        "ingestion_contention": latest["observation_versions"] > status["observation_versions"],
        "initial_rows": status["observation_versions"],
        "final_rows": latest["observation_versions"],
        "pinned_scope": query,
        "series": series,
        "results": results,
        "process_samples": resources,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu": subprocess.check_output(["sysctl", "-n", "hw.ncpu"], text=True).strip(),
            "ram_bytes": subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip(),
        },
        "limits": [
            "No 10-million-row API serving claim from this real sample.",
            "End-to-end p95 is not exchange latency or publisher freshness.",
        ],
    }
    (args.output / "api-performance.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "results": results,
                "max_sampled_rss_kib": max(r["rss_kib"] for r in resources),
                "ingestion_contention": report["ingestion_contention"],
            },
            indent=2,
        )
    )
    if any(r["errors"] for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
