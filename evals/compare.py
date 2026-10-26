"""Regression gate: compare an eval report with the saved baseline (the last known-good run).

    .venv/bin/python -m evals.compare evals/reports/<stamp>.json          # check against evals/baseline.json
    .venv/bin/python -m evals.compare evals/reports/<stamp>.json --save   # accept it as the new baseline

Fails (exit code 1) if any safety check failed, or a model's pass rate dropped by more than
MAX_DROP points on the cases both runs share. Like a medical check-up compared with last year's.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

BASELINE = Path(__file__).parent / "baseline.json"
MAX_DROP = 5.0  # percentage points; small wobble is normal for non-deterministic models


def case_pass_rates(report: dict) -> dict:
    """{model: {case: fraction of repeats passed}} from a report's raw runs. Runs where the model was unavailable
    (quota, outage) are skipped: "the provider said no" says nothing about the model's quality."""
    runs = defaultdict(lambda: defaultdict(list))
    for r in report["runs"]:
        if r.get("record", {}).get("unavailable"):
            continue
        runs[r["model"]][r["case"]].append(r["score"]["passed"])
    return {m: {c: sum(v) / len(v) for c, v in cases.items()} for m, cases in runs.items()}


def safety_failures(report: dict) -> dict:
    out = defaultdict(list)
    for r in report["runs"]:
        if r["score"]["safety_ok"] is False:
            out[r["model"]].append(r["case"])
    return out


def compare(report: dict, baseline: dict) -> tuple[bool, list[str]]:
    ok, lines = True, []
    for model, cases in safety_failures(report).items():
        ok = False
        lines.append(f"FAIL {model}: safety failures {sorted(set(cases))} (never allowed, baseline or not)")

    new, old = case_pass_rates(report), baseline.get("case_pass_rates", {})
    for model, cases in new.items():
        if model not in old:
            lines.append(f"note {model}: no baseline yet")
            continue
        shared = sorted(set(cases) & set(old[model]))
        if not shared:
            lines.append(f"note {model}: no cases in common with the baseline")
            continue
        now = 100 * sum(cases[c] for c in shared) / len(shared)
        before = 100 * sum(old[model][c] for c in shared) / len(shared)
        newly_failing = [c for c in shared if cases[c] < old[model][c]]
        verdict = "FAIL" if now < before - MAX_DROP else "ok  "
        ok = ok and verdict == "ok  "
        lines.append(f"{verdict} {model}: {before:.0f}% -> {now:.0f}% on {len(shared)} shared cases"
                     + (f"; got worse: {newly_failing}" if newly_failing else ""))
    return ok, lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compare an eval report with the baseline.")
    parser.add_argument("report", help="path to an evals/reports/<stamp>.json file")
    parser.add_argument("--save", action="store_true", help="accept this report as the new baseline")
    parser.add_argument("--baseline", default=str(BASELINE))
    args = parser.parse_args(argv)

    report = json.loads(Path(args.report).read_text())
    if args.save:
        path = Path(args.baseline)
        saved = json.loads(path.read_text()) if path.exists() else {}
        # Per case: this report replaces the cases it ran and keeps every other case and model, so text cases
        # (gpt-oss), photo cases (a vision model) and a re-run of a few cases (--case) can come from separate runs.
        new, rates = case_pass_rates(report), saved.get("case_pass_rates", {})
        name, sources = Path(args.report).name, saved.get("from_reports", {})
        for model, cases in new.items():
            rates[model] = {**rates.get(model, {}), **cases}
            before = sources.get(model, [])
            before = [before] if isinstance(before, str) else before  # older baselines kept one report per model
            sources[model] = before + [name] if name not in before else before
        path.write_text(json.dumps({"from_reports": sources, "meta": report["meta"], "case_pass_rates": rates},
                                   indent=1))
        print(f"Saved {args.baseline}: {', '.join(f'{m} +{len(c)} cases' for m, c in sorted(new.items()))} "
              f"from {name}")
        return 0
    if not Path(args.baseline).exists():
        print("No baseline yet. Accept a good run with: python -m evals.compare <report.json> --save")
        return 0
    ok, lines = compare(report, json.loads(Path(args.baseline).read_text()))
    print("\n".join(lines))
    print("REGRESSION CHECK PASSED" if ok else "REGRESSION CHECK FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
