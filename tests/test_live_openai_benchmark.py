from scripts.live_openai_benchmark import EvalCase, build_cases, normalize_answer, summarize


def test_live_case_generation_is_deterministic() -> None:
    assert build_cases(8) == build_cases(8)
    assert {case.category for case in build_cases(8)} == {
        "arithmetic",
        "counting",
        "sorting",
        "string_reverse",
    }


def test_answer_normalization_removes_wrapping_only() -> None:
    assert normalize_answer('  "Answer  42"  ') == "answer 42"
    assert normalize_answer("```text\nABC\n```") == "abc"


def test_summary_recomputes_live_metrics() -> None:
    cases = [EvalCase("case-000", "arithmetic", "prompt", "42")]
    observations = [
        {
            "case_id": "case-000",
            "status": "completed",
            "normalized_output": "42",
            "solved": True,
            "latency_seconds": 1.0 + repetition,
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 0,
                "output_tokens": 2,
                "total_tokens": 12,
            },
        }
        for repetition in range(2)
    ]
    replay = {
        "recorded_traces": 1,
        "fresh_replays": 1,
        "matched_replays": 1,
        "divergent_replays": 0,
        "divergence_rate_percent": 0.0,
        "rows": [],
    }

    result = summarize(
        cases,
        observations,
        replay,
        input_price_per_million=0.25,
        output_price_per_million=2.0,
    )

    assert result["calls"] == 2
    assert result["solve_rate_percent"] == 100.0
    assert result["stable_cases"] == 1
