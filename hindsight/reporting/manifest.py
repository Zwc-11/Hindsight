"""Run manifests for Hindsight outputs."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from hindsight.core.hashing import stable_hash


class RunManifest(BaseModel):
    """Immutable manifest identifying one Hindsight run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine_version: str = Field(min_length=1)
    git_sha: str = Field(min_length=1)
    data_content_hash: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    seed: int
    run_id: str = Field(min_length=1)

    @classmethod
    def build(
        cls,
        *,
        engine_version: str,
        git_sha: str,
        data_content_hash: str,
        config_hash: str,
        seed: int,
    ) -> RunManifest:
        run_id = stable_hash(
            {
                "engine_version": engine_version,
                "git_sha": git_sha,
                "data_content_hash": data_content_hash,
                "config_hash": config_hash,
                "seed": seed,
            }
        )
        return cls(
            engine_version=engine_version,
            git_sha=git_sha,
            data_content_hash=data_content_hash,
            config_hash=config_hash,
            seed=seed,
            run_id=run_id,
        )


def current_git_sha(cwd: Path) -> str:
    # Fallback reason: git metadata is an external command/repository dependency.
    # If git is unavailable, the manifest must still be written with the explicit
    # spec-required sentinel so the run remains auditable.
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return completed.stdout.strip()
