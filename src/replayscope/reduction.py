from __future__ import annotations

import math
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from replayscope.canonical import canonical_json, sha256_bytes
from replayscope.cas import LocalCAS
from replayscope.models import FileEntry, WorkspaceManifest
from replayscope.workspace import restore_workspace


class ReductionPackage(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    tool_returns: dict[str, Any] = Field(default_factory=dict)
    files: list[FileEntry] = Field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.inputs) + len(self.tool_returns) + len(self.files)


class ReductionResult(BaseModel):
    status: str
    signature: str
    original_size: int
    minimal_size: int
    size_reduction: float
    trials: int
    cache_hits: int
    package: ReductionPackage | None = None


FailureOracle = Callable[[ReductionPackage, Path], str | None]
TrialSink = Callable[[str, ReductionPackage, bool, str | None, float], None]


@dataclass
class FailureReducer:
    cas: LocalCAS
    oracle: FailureOracle
    required_signature: str
    trial_sink: TrialSink | None = None
    stability_runs: int = 2
    _cache: dict[str, tuple[bool, str | None]] = field(default_factory=dict)
    trials: int = 0
    cache_hits: int = 0

    def reduce(self, package: ReductionPackage) -> ReductionResult:
        original_size = package.size
        if original_size == 0 or not self._reproduces(package, bypass_cache=True):
            return ReductionResult(
                status="inconclusive",
                signature=self.required_signature,
                original_size=original_size,
                minimal_size=original_size,
                size_reduction=0,
                trials=self.trials,
                cache_hits=self.cache_hits,
            )
        units = self._units(package)
        minimal = self._ddmin(units)
        result_package = self._package(minimal)
        return ReductionResult(
            status="succeeded",
            signature=self.required_signature,
            original_size=original_size,
            minimal_size=result_package.size,
            size_reduction=(original_size - result_package.size) / original_size,
            trials=self.trials,
            cache_hits=self.cache_hits,
            package=result_package,
        )

    def _ddmin(self, units: list[tuple[str, str, Any]]) -> list[tuple[str, str, Any]]:
        current = units
        partitions = 2
        while len(current) >= 2:
            chunk_size = math.ceil(len(current) / partitions)
            reduced = False
            for start in range(0, len(current), chunk_size):
                complement = current[:start] + current[start + chunk_size :]
                if complement and self._reproduces(self._package(complement)):
                    current = complement
                    partitions = max(2, partitions - 1)
                    reduced = True
                    break
            if not reduced:
                if partitions >= len(current):
                    break
                partitions = min(len(current), partitions * 2)
        return current

    def _reproduces(self, package: ReductionPackage, *, bypass_cache: bool = False) -> bool:
        candidate_hash = sha256_bytes(canonical_json(package.model_dump(mode="json")))
        if not bypass_cache and candidate_hash in self._cache:
            self.cache_hits += 1
            return self._cache[candidate_hash][0]
        observed: list[str | None] = []
        started = time.perf_counter()
        for _ in range(self.stability_runs):
            with tempfile.TemporaryDirectory(prefix="replayscope-reduce-") as directory:
                workspace = Path(directory) / "workspace"
                restore_workspace(WorkspaceManifest(files=package.files), workspace, self.cas)
                observed.append(self.oracle(package, workspace))
        elapsed = (time.perf_counter() - started) * 1000
        reproduces = all(item == self.required_signature for item in observed)
        signature = observed[0] if len(set(observed)) == 1 else None
        self._cache[candidate_hash] = (reproduces, signature)
        self.trials += 1
        if self.trial_sink:
            self.trial_sink(candidate_hash, package, reproduces, signature, elapsed)
        return reproduces

    @staticmethod
    def _units(package: ReductionPackage) -> list[tuple[str, str, Any]]:
        units = [("input", key, value) for key, value in package.inputs.items()]
        units.extend(("tool", key, value) for key, value in package.tool_returns.items())
        units.extend(("file", entry.path, entry) for entry in package.files)
        return units

    @staticmethod
    def _package(units: list[tuple[str, str, Any]]) -> ReductionPackage:
        return ReductionPackage(
            inputs={key: value for kind, key, value in units if kind == "input"},
            tool_returns={key: value for kind, key, value in units if kind == "tool"},
            files=[value for kind, _key, value in units if kind == "file"],
        )
