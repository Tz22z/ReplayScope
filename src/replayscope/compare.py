from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from replayscope.models import Divergence, DivergenceKind


@dataclass(frozen=True)
class ComparisonPolicy:
    ignored_paths: frozenset[str] = field(default_factory=frozenset)
    numeric_tolerance: float = 0.0


def compare_values(
    expected: Any,
    actual: Any,
    *,
    sequence: int,
    path: str = "$",
    policy: ComparisonPolicy | None = None,
) -> list[Divergence]:
    policy = policy or ComparisonPolicy()
    if path in policy.ignored_paths:
        return []
    if type(expected) is not type(actual):
        return [_div(sequence, DivergenceKind.TYPE, path, expected, actual, "value types differ")]
    if isinstance(expected, dict):
        divergences: list[Divergence] = []
        for key in sorted(expected.keys() - actual.keys()):
            child = f"{path}.{key}"
            if child not in policy.ignored_paths:
                divergences.append(
                    _div(
                        sequence,
                        DivergenceKind.MISSING,
                        child,
                        expected[key],
                        None,
                        "key is missing",
                    )
                )
        for key in sorted(actual.keys() - expected.keys()):
            child = f"{path}.{key}"
            if child not in policy.ignored_paths:
                divergences.append(
                    _div(
                        sequence,
                        DivergenceKind.UNEXPECTED,
                        child,
                        None,
                        actual[key],
                        "unexpected key",
                    )
                )
        for key in sorted(expected.keys() & actual.keys()):
            divergences.extend(
                compare_values(
                    expected[key],
                    actual[key],
                    sequence=sequence,
                    path=f"{path}.{key}",
                    policy=policy,
                )
            )
        return divergences
    if isinstance(expected, list):
        divergences = []
        shared = min(len(expected), len(actual))
        for index in range(shared):
            divergences.extend(
                compare_values(
                    expected[index],
                    actual[index],
                    sequence=sequence,
                    path=f"{path}[{index}]",
                    policy=policy,
                )
            )
        if len(expected) != len(actual):
            divergences.append(
                _div(
                    sequence,
                    DivergenceKind.VALUE,
                    f"{path}.length",
                    len(expected),
                    len(actual),
                    "array lengths differ",
                )
            )
        return divergences
    if (
        isinstance(expected, (int, float))
        and not isinstance(expected, bool)
        and abs(expected - actual) <= policy.numeric_tolerance
    ):
        return []
    if expected != actual:
        return [_div(sequence, DivergenceKind.VALUE, path, expected, actual, "values differ")]
    return []


def _div(sequence, kind, path, expected, actual, message) -> Divergence:
    return Divergence(
        event_sequence=sequence,
        kind=kind,
        path=path,
        expected=expected,
        actual=actual,
        message=message,
    )
