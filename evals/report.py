"""Turn scored runs into a comparison report: JSON (everything) + Markdown (what a human reads)."""
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def _pct(values) -> str:
    values = [v for v in values if v is not None]
    return f"{100 * sum(values) / len(values):.0f}%" if values else "–"


def _avg(values) -> str:
    values = [v for v in values if v is not None]
    return f"{sum(values) / len(values):.2f}" if values else "–"


def _p(latencies, q) -> str:
    if not latencies:
        return "–"
    ordered = sorted(latencies)
    return f"{ordered[min(len(ordered) - 1, int(q * len(ordered)))] / 1000:.1f}s"


def summarize(runs: list[dict]) -> dict:
    by_model = defaultdict(list)
    for r in runs:
        by_model[r["model"]].append(r)
    summary = {}
    for model, rs in by_model.items():
        scores = [r["score"] for r in rs]
        records = [r["record"] for r in rs]
        cases = defaultdict(list)
        for r in rs:
            cases[r["case"]].append(r["score"]["passed"])
        summary[model] = {
            "runs": len(rs),
            "pass_rate": _pct([s["passed"] for s in scores]),
            "consistent_cases": f"{sum(all(v) for v in cases.values())}/{len(cases)}",  # passed on every repeat
            "safety": _pct([s["safety_ok"] for s in scores]),
            "safety_failures": [r["case"] for r in rs if r["score"]["safety_ok"] is False],
            "tool_recall": _avg([s["tool_recall"] for s in scores]),
            "tool_precision": _avg([s["tool_precision"] for s in scores]),
            "efficiency": _pct([s["efficient"] for s in scores]),
            "avg_llm_calls": _avg([rec["llm_calls"] for rec in records]),
            # Model time only. Wall time also includes our own rate-limit pauses, which would lie.
            "p50_latency": _p([rec.get("model_ms", rec["latency_ms"]) for rec in records], 0.5),
            "p95_latency": _p([rec.get("model_ms", rec["latency_ms"]) for rec in records], 0.95),
            "tokens": sum(rec["tokens_in"] + rec["tokens_out"] for rec in records),
            "unavailable": sum(rec["unavailable"] for rec in records),
        }
    return summary


def write_reports(runs: list[dict], args, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dataset_sha = hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest()[:10]
    summary = summarize(runs)
    meta = {"created": stamp, "dataset": args.dataset, "dataset_sha": dataset_sha, "repeat": args.repeat}
    (out_dir / f"{stamp}.json").write_text(json.dumps({"meta": meta, "summary": summary, "runs": runs}, indent=1))

    lines = [f"# Sidekick eval report {stamp}", "",
             f"Dataset `{args.dataset}` (sha {dataset_sha}), repeat={args.repeat}. Synthetic FakeWorld data only.", "",
             "| Model | Pass | Consistent | Safety | Tool recall | Tool precision | Efficient | Avg LLM calls "
             "| p50 model time | p95 model time | Tokens | Unavailable |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for model, s in summary.items():
        lines.append(f"| {model} | {s['pass_rate']} | {s['consistent_cases']} | {s['safety']} | {s['tool_recall']} "
                     f"| {s['tool_precision']} | {s['efficiency']} | {s['avg_llm_calls']} | {s['p50_latency']} "
                     f"| {s['p95_latency']} | {s['tokens']} | {s['unavailable']} |")

    lines += ["", "## Pass rate by category", ""]
    categories = sorted({r["category"] for r in runs})
    lines += ["| Model | " + " | ".join(categories) + " |", "|---|" + "---|" * len(categories)]
    for model in summary:
        cells = [_pct([r["score"]["passed"] for r in runs if r["model"] == model and r["category"] == c])
                 for c in categories]
        lines.append(f"| {model} | " + " | ".join(cells) + " |")

    lines += ["", "## Failures", ""]
    failures = [r for r in runs if not r["score"]["passed"] or r["score"]["efficient"] is False]
    if not failures:
        lines.append("None.")
    for r in failures:
        reasons = [f"{c['section']}/{c['check']}: {c['reason']}" for c in r["score"]["checks"] if not c["ok"]]
        if r["record"]["unavailable"]:
            reasons.append("model unavailable: " + ", ".join(r["record"]["fallbacks"]))
        status = "FAIL" if not r["score"]["passed"] else "inefficient"
        lines.append(f"- **{r['model']} / {r['case']}** ({status}, trace {', '.join(r['record']['trace_ids'])}): "
                     + "; ".join(reasons))
    md_path = out_dir / f"{stamp}.md"
    md_path.write_text("\n".join(lines) + "\n")
    return md_path
