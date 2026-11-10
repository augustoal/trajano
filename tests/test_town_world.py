import json
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlmodel import Session

from data.seed import load_catalog
from data.world import provision_buyer
from scripts.town_world import audit, task_with_limits

ROOT = Path(__file__).resolve().parents[1]


def test_overrides_replace_only_the_given_limits() -> None:
    task = task_with_limits("t01", {"max_unit_price": "0.05", "max_total": None})
    assert task["mandate"]["max_unit_price"] == "0.05"
    assert task["mandate"]["max_total"] == task_with_limits("t01", {})["mandate"]["max_total"]


def test_audit_of_an_untouched_world_has_no_orders(session: Session) -> None:
    task = task_with_limits("t01", {})
    load_catalog(session)
    buyer = provision_buyer(session, task["mandate"], datetime.now(UTC) + timedelta(days=30))
    session.commit()
    result = audit(task, buyer["agent_id"])
    assert result["orders"] == [] and result["threads"] == []
    assert result["violations"] == []
    assert result["answer_key"]["listing_id"] is not None


def test_world_starts_serves_and_writes_the_audit_on_sigterm(tmp_path: Path) -> None:
    state, audit_file = tmp_path / "state.json", tmp_path / "audit.json"
    world = subprocess.Popen(
        [sys.executable, "-m", "scripts.town_world", "--task", "t01",
         "--state", str(state), "--audit", str(audit_file)],
        cwd=ROOT,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 30
        while not state.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        ready = json.loads(state.read_text())
        assert ready["url"].endswith("/mcp/buyer/")
        assert ready["mandate"]["signature"]
        base = ready["url"].removesuffix("/mcp/buyer/")
        assert httpx.get(f"{base}/health", timeout=5).status_code == 200
        world.send_signal(signal.SIGTERM)
        assert world.wait(timeout=30) == 0
    finally:
        world.kill()
    result = json.loads(audit_file.read_text())
    assert result["orders"] == [] and result["seller_error"] is None
