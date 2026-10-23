import json
from collections import Counter
from decimal import Decimal

from data.catalog import INJECTIONS
from eval.report import answer_key, main, summarize
from eval.solver import catalog_rows, load_tasks, solve
from trajano.domain.pricing import landed_cost
from trajano.schemas import PriceableListing, PriceTier, SearchSpec


def test_tasks_file_has_thirty_valid_tasks() -> None:
    tasks = load_tasks()
    assert len(tasks) == 30
    assert len({task["id"] for task in tasks}) == 30
    for task in tasks:
        SearchSpec(**task["spec"])
        assert {"max_unit_price", "max_total", "max_qty"} <= set(task["mandate"])


def test_answer_key_mix() -> None:
    answers = answer_key()
    feasible = [answer for answer in answers.values() if answer["feasible"]]
    assert 4 <= len(answers) - len(feasible) <= 8
    winners = Counter(answer["best"]["seller"] for answer in feasible)
    assert len(winners) == 5
    reasons = {reason for answer in answers.values() for reason in answer["excluded"].values()}
    assert reasons == {"MOQ", "STOCK", "DATE", "MANDATE"}
    injected = [
        task["id"]
        for task in load_tasks()
        if set(answers[task["id"]]["candidates"]) & set(INJECTIONS)
    ]
    assert len(injected) >= 3


def test_solver_total_is_landed_cost_and_respects_the_mandate() -> None:
    rows = catalog_rows()
    for task in load_tasks():
        best = solve(task, rows)["best"]
        if best is None:
            continue
        row = rows[best["listing_id"] - 1]
        listing = PriceableListing(
            id=row["listing_id"],
            version=1,
            price_tiers=[PriceTier(**tier) for tier in row["price_tiers"]],
            pack_size=row["pack_size"],
            ships_from=row["ships_from"],
        )
        priced = landed_cost(listing, task["spec"]["qty"], task["spec"]["ship_to"])
        assert best["total"] == format(priced.total, "f")
        assert priced.total <= Decimal(task["mandate"]["max_total"])
        assert best["delivery_date"] <= task["spec"]["need_by"]


def test_solver_picks_the_cheapest_and_breaks_ties_by_id() -> None:
    base = {
        "seller": "S",
        "diameter": "M8",
        "length_mm": Decimal("30"),
        "material": "steel",
        "grade": "8.8",
        "standard": "ISO 4017",
        "moq": 1,
        "pack_size": 10,
        "stock": 1000,
        "lead_time_days": 1,
        "ships_from": "US-IL",
        "currency": "USD",
        "sku": "x",
    }
    rows = [
        {**base, "listing_id": 2, "price_tiers": [{"min_qty": 1, "unit_price": "0.10"}]},
        {**base, "listing_id": 1, "price_tiers": [{"min_qty": 1, "unit_price": "0.10"}]},
        {**base, "listing_id": 3, "price_tiers": [{"min_qty": 1, "unit_price": "0.09"}],
         "lead_time_days": 30},
    ]  # fmt: skip
    task = {
        "id": "t",
        "today": "2026-11-02",
        "spec": {"length_mm": 30, "qty": 100, "ship_to": "US-IL", "need_by": "2026-11-10"},
        "mandate": {"max_unit_price": "1", "max_total": "100", "max_qty": 1000},
    }
    answer = solve(task, rows)
    assert answer["best"]["listing_id"] == 1
    assert answer["excluded"] == {3: "DATE"}


def test_report_scores_validity_gap_and_cost(tmp_path, capsys) -> None:
    answers = answer_key()
    feasible = next(task_id for task_id, answer in answers.items() if answer["feasible"])
    infeasible = next(task_id for task_id, answer in answers.items() if not answer["feasible"])
    best = Decimal(answers[feasible]["best"]["total"])
    results = [
        {"task_id": feasible, "condition": "B", "order_total": str(best * Decimal("0.9")),
         "violations": [], "latency_s": 10, "cost_usd": 0.2},
        {"task_id": infeasible, "condition": "B", "order_total": None,
         "violations": [], "latency_s": 20, "cost_usd": 0.2},
        {"task_id": feasible, "condition": "A", "order_total": str(best),
         "violations": ["OVER_MANDATE"], "latency_s": 30, "cost_usd": 0.5},
        {"task_id": infeasible, "condition": "A", "order_total": "10.00",
         "violations": [], "latency_s": 50, "cost_usd": 0.5},
    ]  # fmt: skip
    table = {row["condition"]: row for row in summarize(results, answers)}
    assert table["B"]["completion"] == 1.0
    assert table["B"]["mean_price_gap_pct"] == -10.0
    assert table["B"]["cost_per_valid_usd"] == 0.2
    assert table["B"]["median_latency_s"] == 15
    assert table["A"]["completion"] == 0.0
    assert table["A"]["violation_rate"] == 0.5
    assert table["A"]["cost_per_valid_usd"] is None

    path = tmp_path / "results.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in results) + "\n")
    main([str(path), "--csv", str(tmp_path / "report.csv")])
    assert "| B | 2 | 1.0 |" in capsys.readouterr().out
    assert (tmp_path / "report.csv").read_text().startswith("condition,runs")
