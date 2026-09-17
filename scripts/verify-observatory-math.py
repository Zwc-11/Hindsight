"""Independent numerical oracles. All inputs here are synthetic, never market claims."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import tempfile
from pathlib import Path

import dcor
import numpy as np
import scipy
from scipy.stats import beta, pearsonr, spearmanr
from sklearn.covariance import ledoit_wolf
from statsmodels.stats.multitest import multipletests


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, default=Path("target/debug/hindsight-observatory"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results: list[dict[str, object]] = []
    rng = np.random.default_rng(20260916)
    with tempfile.TemporaryDirectory(prefix="hindsight-oracles-") as temporary:
        temp = Path(temporary)
        counter = 0

        def call(method: str, value: object, **extra: object) -> object:
            nonlocal counter
            counter += 1
            source = temp / f"input-{counter}.json"
            source.write_text(json.dumps({"method": method, "input": value, **extra}))
            process = subprocess.run(
                [str(args.binary.resolve()), "--root", str(temp / "catalog"), "math", str(source)],
                text=True,
                capture_output=True,
                check=False,
            )
            if process.returncode:
                raise AssertionError(f"{method}: {process.stderr}")
            return json.loads(process.stdout)

        def record(name: str, actual: object, expected: object, tolerance: float = 1e-9) -> None:
            np.testing.assert_allclose(actual, expected, rtol=tolerance, atol=tolerance)
            results.append(
                {
                    "test": name,
                    "passed": True,
                    "max_absolute_error": float(
                        np.max(np.abs(np.asarray(actual) - np.asarray(expected)))
                    ),
                    "tolerance": tolerance,
                }
            )

        for n in (8, 41, 120):
            x = rng.normal(size=n)
            y = 0.4 * x + rng.normal(size=n)
            native = call("pair", {"x": x.tolist(), "y": y.tolist()})
            record(f"pearson-n{n}", native["pearson"], pearsonr(x, y).statistic)
            record(f"spearman-n{n}", native["spearman"], spearmanr(x, y).statistic)
            record(f"distance-n{n}", native["distance"], dcor.distance_correlation(x, y))
            weights = 0.9 ** np.arange(n - 1, -1, -1)
            mx, my = np.average(x, weights=weights), np.average(y, weights=weights)
            expected = np.sum(weights * (x - mx) * (y - my)) / np.sqrt(
                np.sum(weights * (x - mx) ** 2) * np.sum(weights * (y - my) ** 2)
            )
            record(f"exponentially-weighted-n{n}", native["ew"], expected)
        for n, p in ((40, 3), (8, 20), (120, 12), (30, 1)):
            x = rng.normal(size=(n, p)) * np.arange(1, p + 1)
            native = call("covariance", x.tolist())
            cov, shrink = ledoit_wolf(x)
            record(f"ledoit-wolf-covariance-{n}x{p}", native["covariance"], cov)
            record(f"ledoit-wolf-coefficient-{n}x{p}", native["shrinkage"], shrink)
            theta = np.linalg.inv(cov)
            partial = -theta / np.sqrt(np.outer(np.diag(theta), np.diag(theta)))
            np.fill_diagonal(partial, 1.0)
            record(f"partial-correlation-{n}x{p}", native["partial"], partial)
        pvalues = np.r_[rng.uniform(size=90), rng.uniform(0, 0.002, 10)]
        native = call("adjust", pvalues.tolist())
        record("BH-whole-family", native["BH"], multipletests(pvalues, method="fdr_bh")[1])
        record("BY-whole-family", native["BY"], multipletests(pvalues, method="fdr_by")[1])
        config = {
            "phi": 0.8,
            "process_variance": 0.2,
            "initial_variance": 1.0,
            "memory": 10,
            "steps": 24,
            "releases": [
                {
                    "released_at_step": 5,
                    "period_start_step": 1,
                    "period_end_step": 3,
                    "value": 3.0,
                    "aggregation": "sum",
                    "noise_variance": 0.3,
                },
                {
                    "released_at_step": 10,
                    "period_start_step": 7,
                    "period_end_step": 9,
                    "value": -0.5,
                    "aggregation": "mean",
                    "noise_variance": 0.8,
                },
                {
                    "released_at_step": 17,
                    "period_start_step": 16,
                    "period_end_step": 16,
                    "value": 2.0,
                    "aggregation": "last",
                    "noise_variance": 0.4,
                },
            ],
        }
        native = call("filter", config)
        dimension = config["memory"]
        transition = np.zeros((dimension, dimension))
        transition[0, 0] = config["phi"]
        transition[1:, :-1] = np.eye(dimension - 1)
        process_noise = np.zeros((dimension, dimension))
        process_noise[0, 0] = config["process_variance"]
        covariance = config["initial_variance"] * config["phi"] ** np.abs(
            np.subtract.outer(np.arange(dimension), np.arange(dimension))
        )
        mean = np.zeros(dimension)
        oracle = []
        for step in range(config["steps"]):
            if step:
                mean = transition @ mean
                covariance = transition @ covariance @ transition.T + process_noise
            for release in config["releases"]:
                if release["released_at_step"] != step:
                    continue
                loading = np.zeros(dimension)
                if release["aggregation"] == "last":
                    loading[step - release["period_end_step"]] = 1.0
                else:
                    loading[
                        step - release["period_end_step"] : step - release["period_start_step"] + 1
                    ] = 1.0
                    if release["aggregation"] == "mean":
                        loading /= release["period_end_step"] - release["period_start_step"] + 1
                gain = (
                    covariance
                    @ loading
                    / (loading @ covariance @ loading + release["noise_variance"])
                )
                mean += gain * (release["value"] - loading @ mean)
                operator = np.eye(dimension) - np.outer(gain, loading)
                covariance = (
                    operator @ covariance @ operator.T
                    + np.outer(gain, gain) * release["noise_variance"]
                )
            oracle.append([mean[0], covariance[0, 0]])
        record(
            "ragged-aggregate-Kalman-full-matrix-oracle",
            [[p["mean"], p["variance"]] for p in native],
            oracle,
        )
        # Assess calibration of a cyclic-shift null; this is not a general validity guarantee.
        rejections = 0
        replicates = 120
        for _ in range(replicates):
            innovations = rng.normal(size=(2, 264))
            values = np.zeros_like(innovations)
            for t in range(1, 264):
                values[:, t] = 0.75 * values[:, t - 1] + innovations[:, t]
            x, y = values[:, -64:]
            result = call("shift_null", {"x": x.tolist(), "y": y.tolist()})
            rejections += result["p_value"] <= 0.05
        low = float(beta.ppf(0.025, rejections, replicates - rejections + 1)) if rejections else 0.0
        high = (
            float(beta.ppf(0.975, rejections + 1, replicates - rejections))
            if rejections < replicates
            else 1.0
        )
        null = {
            "simulation": "independent AR(1), phi=.75, 200 burn-in, 64 evaluation points",
            "replicates": replicates,
            "rejections_at_0_05": rejections,
            "observed_fraction": rejections / replicates,
            "binomial_interval_95": [low, high],
            "inference": (
                "Calibration diagnostic only. Cyclic shifts are not claimed exact "
                "for ordinary AR time series. No empirical p/q values are served "
                "for unverified source assumptions."
            ),
        }
    report = {
        "synthetic_only": True,
        "numerical_checks": results,
        "checks_passed": len(results),
        "null_calibration": null,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "architecture": platform.machine(),
        },
        "unverified": [
            "Real-data causal identification",
            "Profitable strategy",
            "General-purpose maximum-likelihood mixed-frequency nowcaster",
        ],
    }
    (args.output / "numerical-reference-results.json").write_text(
        json.dumps(report, indent=2) + "\n"
    )
    print(json.dumps({"passed": len(results), "null_calibration": null}, indent=2))


if __name__ == "__main__":
    main()
