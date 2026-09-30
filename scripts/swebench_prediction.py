"""Convert one RepoPilot issue report into a SWE-bench prediction JSONL row."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def build_prediction(report_path: Path, instance_id: str, model_name: str,
                     require_resolved: bool = True) -> dict:
    report_path = Path(report_path).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if require_resolved and report.get("status") != "resolved":
        raise ValueError(f"RepoPilot report is not resolved: {report.get('status')}")
    raw_diff_path = report.get("diff_path")
    if not raw_diff_path:
        raise ValueError("RepoPilot report does not contain diff_path")
    diff_path = Path(raw_diff_path)
    if not diff_path.is_absolute():
        diff_path = report_path.parent / diff_path
    patch = diff_path.read_text(encoding="utf-8")
    if not patch.strip():
        raise ValueError("RepoPilot patch is empty")
    return {
        "instance_id": instance_id,
        "model_name_or_path": model_name,
        "model_patch": patch,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--model-name", default="repopilot-deepseek-stage2")
    parser.add_argument("--allow-nonresolved", action="store_true",
                        help="emit a non-empty partial patch even when RepoPilot did not resolve internally")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prediction = build_prediction(
        args.report, args.instance_id, args.model_name,
        require_resolved=not args.allow_nonresolved,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(prediction, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
