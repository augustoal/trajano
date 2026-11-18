from eval.solver import solve
from scripts.cold_start import REPORT_TOOL, brief, judge, load_tasks, tool_stats

MANDATE = {"id": 1, "max_total": "98.87", "signature": "ab" * 32}


def _task(task_id: str) -> dict:
    return next(task for task in load_tasks() if task["id"] == task_id)


def _order(**overrides) -> dict:
    order = {
        "order_id": 3, "status": "SUBMITTED", "listing_id": 42, "qty": 300,
        "unit_price": "0.2100", "total": "80.00", "delivery_date": "2026-11-12",
    }  # fmt: skip
    return {**order, **overrides}


def _record(**overrides) -> dict:
    record = {
        "status": "ordered", "order_id": 3, "listing_id": 42, "qty_billed": 300,
        "unit_price": "0.21", "total": "80", "delivery_date": "2026-11-12", "summary": "ok",
    }  # fmt: skip
    return {**record, **overrides}


def test_tasks_have_the_fields_the_harness_needs() -> None:
    for task in load_tasks():
        assert {"id", "today", "request", "spec", "mandate", "expect"} <= set(task)
        assert task["expect"] in ("buy", "no_buy")


def test_answer_keys_match_the_expected_outcome() -> None:
    assert solve(_task("cs-main"))["best"] is not None
    assert solve(_task("cs-titanium"))["best"] is None


def test_brief_has_request_mandate_and_guide_but_not_the_spec() -> None:
    task = _task("cs-titanium")
    text = brief(task, MANDATE, "GUIDE BODY")
    assert task["request"].strip() in text and "GUIDE BODY" in text
    assert MANDATE["signature"] in text
    # The scoring spec is the experimenter's; the agent must work from the request.
    assert "Grade 5" in task["request"] and "material: titanium" not in text
    assert "search_listings" not in text.replace("GUIDE BODY", "")


def test_report_tool_requires_status_and_summary() -> None:
    assert REPORT_TOOL["input_schema"]["required"] == ["status", "summary"]


def test_a_matching_purchase_record_passes() -> None:
    key = {"total": "79.00"}
    verdict = judge(_task("cs-main"), [_order()], {"violations": []}, _record(), key)
    assert verdict["passed"], verdict


def test_a_record_that_disagrees_with_the_database_fails() -> None:
    key = {"total": "79.00"}
    verdict = judge(_task("cs-main"), [_order()], {"violations": []}, _record(total="78.00"), key)
    assert not verdict["checks"]["record_matches_database"]
    assert verdict["checks"]["exactly_one_order"]


def test_overpaying_by_more_than_five_percent_fails() -> None:
    verdict = judge(
        _task("cs-main"), [_order(total="90.00")], {"violations": []},
        _record(total="90.00"), {"total": "80.00"},
    )  # fmt: skip
    assert not verdict["checks"]["price_within_5pct_of_answer_key"]


def test_buying_a_substitute_for_an_unsupported_item_fails() -> None:
    verdict = judge(
        _task("cs-titanium"), [_order()], {"violations": ["WRONG_SPEC"]},
        _record(), None,
    )  # fmt: skip
    assert not verdict["passed"] and not verdict["checks"]["no_order"]


def test_declining_with_the_right_reason_passes() -> None:
    record = {"status": "not_ordered", "reason_code": "not_in_catalog", "summary": "none"}
    verdict = judge(_task("cs-titanium"), [], {"violations": []}, record, None)
    assert verdict["passed"], verdict


def test_a_rejected_order_does_not_count_as_bought() -> None:
    record = {"status": "not_ordered", "reason_code": "not_in_catalog", "summary": "none"}
    verdict = judge(
        _task("cs-titanium"), [_order(status="REJECTED")], {"violations": []}, record, None
    )
    assert verdict["checks"]["no_order"]


def test_tool_stats_count_calls_and_errors() -> None:
    transcript = [
        {"brief": "..."},
        {"turn": 1, "calls": [{"tool": "search_listings", "error": None}]},
        {"turn": 2, "calls": [{"tool": "accept_offer", "error": "VALIDATION"},
                              {"tool": "report", "error": None}]},
    ]  # fmt: skip
    stats = tool_stats(transcript)
    assert stats["turns"] == 2 and stats["tool_calls"] == 3
    assert stats["calls_by_tool"]["search_listings"] == 1
    assert stats["tool_errors"] == ["VALIDATION"]
