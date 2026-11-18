"""Print the HW9 cold-start evidence: one line per run and every search the agent made.

uv run python -m scripts.hw9_summary
"""

import json
from pathlib import Path

DIR = Path(__file__).resolve().parents[1] / "eval" / "results" / "hw9"
FIELDS = ("diameter", "length_mm", "material", "grade", "standard", "qty", "ship_to")


def main() -> None:
    for label in ("before", "after"):
        rows_file = DIR / f"{label}.jsonl"
        if not rows_file.exists():
            continue
        rows = [json.loads(line) for line in rows_file.read_text().splitlines() if line]
        turns = [
            json.loads(line)
            for line in (DIR / f"{label}.transcripts.jsonl").read_text().splitlines()
            if line
        ]
        print(f"===== {label} =====")
        for row, transcript in zip(rows, turns, strict=True):
            rep = row["report"]
            print(
                f"{row['task_id']} run {row['run']}: {'PASS' if row['passed'] else 'FAIL'} "
                f"| total {row['order_total']} key {(row['answer_key'] or {}).get('total')} "
                f"| searches {row['calls_by_tool'].get('search_listings', 0)} "
                f"| calls {row['tool_calls']} | turns {row['turns']} | {row['latency_s']}s "
                f"| report {rep.get('status')}/{rep.get('reason_code')} "
                f"| record {rep.get('order_id')},{rep.get('listing_id')},"
                f"{rep.get('qty_billed')},{rep.get('total')},{rep.get('delivery_date')}"
            )
            for turn in transcript["turns"]:
                for call in turn.get("calls", []):
                    if call["tool"] != "search_listings":
                        continue
                    args = {k: call["arguments"].get(k) for k in FIELDS if k in call["arguments"]}
                    try:
                        body = json.loads(call["result"])
                        got = (
                            f"hits={len(body.get('hits', []))} "
                            f"excluded={len(body.get('excluded', []))} "
                            f"hints={[h.get('field') for h in body.get('hints', [])]}"
                        )
                    except json.JSONDecodeError:
                        got = "result cut in transcript"
                    print(f"    search {args} -> {got}")
            print(f"    summary: {rep.get('summary', '')[:300]}")


if __name__ == "__main__":
    main()
