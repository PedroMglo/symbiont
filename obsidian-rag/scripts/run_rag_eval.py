#!/usr/bin/env python3
"""Retrieval-quality eval gate for obsidian-rag.

Runs the golden-query eval harness and (optionally) enforces minimum quality
thresholds, so it can be used as a CI/local regression gate.

Modes:
  - Direct store (default): queries the local VectorStore + embedder.
  - HTTP: when ``--api-url`` (or ``RAG_API_URL``) is set, queries the running
    FastAPI service via /query and /query/code (exercises hybrid search).

Usage:
    python scripts/run_rag_eval.py
    python scripts/run_rag_eval.py --api-url https://localhost:8484
    python scripts/run_rag_eval.py --min-recall 0.5 --min-mrr 0.4
    python scripts/run_rag_eval.py --baseline reports/evals/rag_retrieval_latest.json

Exit codes:
    0  thresholds met (or no thresholds given)
    1  thresholds not met / regression detected
    2  setup error (missing golden queries, etc.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Resolve repo paths: this file lives in obsidian-rag/scripts/
_SCRIPT_DIR = Path(__file__).resolve().parent
_RAG_ROOT = _SCRIPT_DIR.parent              # obsidian-rag/
_WORKSPACE_ROOT = _RAG_ROOT.parents[1]     # ai-local/

_DEFAULT_GOLDEN = _WORKSPACE_ROOT / "tests" / "rag" / "golden_queries.json"
_FALLBACK_GOLDEN = _RAG_ROOT / "evals" / "golden.sample.json"
_DEFAULT_OUTPUT = _WORKSPACE_ROOT / "reports" / "evals" / "rag_retrieval_latest.json"


def _resolve_golden(path_arg: str | None) -> Path:
    if path_arg:
        return Path(path_arg)
    if _DEFAULT_GOLDEN.exists():
        return _DEFAULT_GOLDEN
    return _FALLBACK_GOLDEN


def main() -> int:
    parser = argparse.ArgumentParser(description="obsidian-rag retrieval eval gate")
    parser.add_argument("--golden", help="Path to golden queries JSON")
    parser.add_argument("--api-url", default=os.environ.get("RAG_API_URL", ""),
                        help="Run against a live API instead of the local store")
    parser.add_argument("--api-key", default=os.environ.get("RAG_API_KEY", ""))
    parser.add_argument("--output", default=str(_DEFAULT_OUTPUT),
                        help="Where to write the JSON report (default: reports/evals/)")
    parser.add_argument("--min-recall", type=float, default=None)
    parser.add_argument("--min-mrr", type=float, default=None)
    parser.add_argument("--min-precision", type=float, default=None)
    parser.add_argument("--baseline", help="Compare against a previous report JSON for regression")
    parser.add_argument("--regression-tolerance", type=float, default=0.05,
                        help="Allowed drop vs baseline before failing (default: 0.05)")
    args = parser.parse_args()

    from retrieval.eval import EvalHarness, load_golden_queries

    golden_path = _resolve_golden(args.golden)
    if not golden_path.exists():
        print(f"ERROR: golden queries not found: {golden_path}", file=sys.stderr)
        return 2

    queries = load_golden_queries(golden_path)
    if not queries:
        print("ERROR: golden query set is empty", file=sys.stderr)
        return 2

    harness = EvalHarness(api_url=args.api_url, api_key=args.api_key)
    report = harness.run(queries)

    print(EvalHarness.report(report))
    print(f"\nGolden set: {golden_path} ({report.total_queries} queries)")

    # Persist the report
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report written to: {out_path}")

    failures: list[str] = []

    if args.min_recall is not None and report.mean_recall < args.min_recall:
        failures.append(f"mean_recall {report.mean_recall:.3f} < {args.min_recall}")
    if args.min_mrr is not None and report.mean_mrr < args.min_mrr:
        failures.append(f"mean_mrr {report.mean_mrr:.3f} < {args.min_mrr}")
    if args.min_precision is not None and report.mean_precision < args.min_precision:
        failures.append(f"mean_precision {report.mean_precision:.3f} < {args.min_precision}")

    # Regression check vs baseline
    if args.baseline:
        base_path = Path(args.baseline)
        if base_path.exists():
            base = json.loads(base_path.read_text(encoding="utf-8"))
            tol = args.regression_tolerance
            for metric in ("mean_recall", "mean_mrr", "mean_precision"):
                base_val = base.get(metric)
                cur_val = getattr(report, metric)
                if base_val is not None and cur_val < base_val - tol:
                    failures.append(
                        f"regression: {metric} {cur_val:.3f} < baseline {base_val:.3f} - tol {tol}"
                    )
        else:
            print(f"NOTE: baseline not found ({base_path}); skipping regression check")

    if failures:
        print("\nEVAL GATE FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nEVAL GATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
