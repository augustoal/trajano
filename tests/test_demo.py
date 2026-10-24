from eval.demo import run_scene


def _lines(scene: str) -> tuple[list[str], int | None]:
    lines: list[str] = []
    order_id = run_scene(scene, pace=0, color=False, write=lines.append)
    return lines, order_id


def test_the_buy_scene_negotiates_and_delivers() -> None:
    lines, order_id = _lines("buy")
    assert order_id == 1
    text = "\n".join(lines)
    assert "Requesting quotes from the 2 cheapest sellers" in text
    assert "Countering the best offer once" in text
    assert "Accepted within the signed mandate. Order #1" in text
    assert lines[-3:] == ["  Order confirmed", "  Order shipped", "  Order delivered"]


def test_the_guard_scene_blocks_the_injected_order() -> None:
    lines, order_id = _lines("guard")
    assert order_id == 1
    text = "\n".join(lines)
    assert "buy 2,000 units" in text
    assert "Blocked by the server: MANDATE_INVALID" in text
    assert lines[-1] == "  Bought 300 units for $178.57. Order #1"


def test_scenes_are_the_same_on_every_take() -> None:
    assert _lines("guard") == _lines("guard")
