"""Unit tests for BudgetPlanner — pure, no mocking needed."""

from auton.budget import BudgetPlanner


def test_record_call_updates_spent():
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(5_000)
    assert p.total_spent == 5_000
    assert p.remaining == 95_000


def test_record_multiple_calls():
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(10_000)
    p.record_call(15_000)
    p.record_call(20_000)
    assert p.total_spent == 45_000
    assert p.remaining == 55_000


def test_pct_used():
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(30_000)
    assert p.pct_used == 30.0


def test_pct_used_zero_budget():
    p = BudgetPlanner(max_budget=0)
    assert p.pct_used == 0.0


def test_remaining_never_negative():
    p = BudgetPlanner(max_budget=10_000)
    p.record_call(15_000)
    assert p.remaining == 0  # clamped at 0


def test_finalize_reserve():
    p = BudgetPlanner(max_budget=200_000)
    assert p.finalize_reserve == 20_000  # 10% of 200K

    p2 = BudgetPlanner(max_budget=200_000, finalize_reserve_ratio=0.05)
    assert p2.finalize_reserve == 10_000  # 5% of 200K


# ---------------------------------------------------------------------------
# estimated_next_cost
# ---------------------------------------------------------------------------


def test_estimated_next_cost_no_data():
    p = BudgetPlanner(max_budget=100_000)
    assert p.estimated_next_cost() == 0  # no data, allow call


def test_estimated_next_cost_single_call():
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(10_000)
    assert p.estimated_next_cost() == 15_000  # 1.5x


def test_estimated_next_cost_two_calls():
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(5_000)
    p.record_call(8_000)
    # max(5000, 8000) * 1.2 = 9600
    assert p.estimated_next_cost() == 9_600


def test_estimated_next_cost_uses_max_of_recent():
    p = BudgetPlanner(max_budget=100_000)
    for cost in [3000, 5000, 4000, 7000, 6000]:
        p.record_call(cost)
    # max of last 5 = 7000, * 1.2 = 8400
    assert p.estimated_next_cost() == 8_400


def test_estimated_next_cost_window_limit():
    """Only last WINDOW_SIZE calls used for estimation."""
    p = BudgetPlanner(max_budget=1_000_000)
    # First call is huge, but outside window
    p.record_call(100_000)
    for _ in range(5):
        p.record_call(5_000)
    # Window is last 5 calls: [5K, 5K, 5K, 5K, 5K]
    # max = 5K, * 1.2 = 6K
    assert p.estimated_next_cost() == 6_000


# ---------------------------------------------------------------------------
# should_finalize
# ---------------------------------------------------------------------------


def test_should_not_finalize_when_budget_healthy():
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(5_000)
    # remaining = 95K, reserve = 10K, est_next = 7500
    # 95K > 7500 + 10K => should not finalize
    assert p.should_finalize() is False


def test_should_finalize_when_budget_tight():
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(85_000)
    # remaining = 15K, reserve = 10K, est_next = 85K * 1.5 = 127500
    # 15K < 127500 + 10K => should finalize
    assert p.should_finalize() is True


def test_should_finalize_gradual_spending():
    """Realistic: many calls of increasing cost."""
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(5_000)
    p.record_call(7_000)
    p.record_call(9_000)
    p.record_call(12_000)
    # total = 33K, remaining = 67K
    # max of last 4 = 12K, * 1.2 = 14400
    # 67K > 14400 + 10K => should NOT finalize
    assert p.should_finalize() is False

    p.record_call(15_000)
    p.record_call(20_000)
    p.record_call(25_000)
    # total = 93K, remaining = 7K
    # max of last 5 = 25K, * 1.2 = 30K
    # 7K < 30K + 10K => SHOULD finalize
    assert p.should_finalize() is True


def test_finalize_is_sticky():
    """Once triggered, should_finalize stays True."""
    p = BudgetPlanner(max_budget=20_000)
    p.record_call(18_000)
    assert p.should_finalize() is True
    assert p.should_finalize() is True  # still True
    assert p.finalize_triggered is True


def test_finalize_not_triggered_initially():
    p = BudgetPlanner(max_budget=100_000)
    assert p.finalize_triggered is False


# ---------------------------------------------------------------------------
# hard_stop
# ---------------------------------------------------------------------------


def test_hard_stop_below_threshold():
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(114_000)
    assert p.hard_stop() is False  # 114% < 115%


def test_hard_stop_at_threshold():
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(116_000)
    assert p.hard_stop() is True  # 116% > 115%


def test_hard_stop_cumulative():
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(60_000)
    assert p.hard_stop() is False
    p.record_call(56_000)  # total = 116K
    assert p.hard_stop() is True


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def test_snapshot_returns_diagnostics():
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(10_000)
    p.record_call(12_000)
    snap = p.snapshot()
    assert snap["total_spent"] == 22_000
    assert snap["max_budget"] == 100_000
    assert snap["remaining"] == 78_000
    assert snap["pct_used"] == 22.0
    assert snap["calls_recorded"] == 2
    assert snap["recent_call_costs"] == [10_000, 12_000]
    assert snap["finalize_triggered"] is False
    assert snap["estimated_next_cost"] == int(12_000 * 1.2)
    assert snap["finalize_reserve"] == 10_000


def test_snapshot_after_finalize():
    p = BudgetPlanner(max_budget=50_000)
    p.record_call(45_000)
    p.should_finalize()  # triggers it
    snap = p.snapshot()
    assert snap["finalize_triggered"] is True


# ---------------------------------------------------------------------------
# Realistic scenarios
# ---------------------------------------------------------------------------


def test_growing_conversation_triggers_finalize():
    """Simulate realistic growing costs — planner catches it."""
    p = BudgetPlanner(max_budget=200_000)
    costs = [8000, 12000, 18000, 25000, 35000, 45000]
    for c in costs:
        p.record_call(c)
    # total = 143K, remaining = 57K
    # max of recent = 45K, est = 45K * 1.2 = 54K
    # reserve = 20K
    # 57K < 54K + 20K = 74K => should finalize
    assert p.should_finalize() is True
    # Finalized at 143K / 200K = 71.5% — well before 100%
    assert p.pct_used < 80


# ---------------------------------------------------------------------------
# Child budget reservation
# ---------------------------------------------------------------------------


def test_reserve_for_child():
    p = BudgetPlanner(max_budget=300_000)
    p.record_call(10_000)
    assert p.remaining == 290_000

    p.reserve_for_child(50_000)
    assert p.reserved_for_children == 50_000
    assert p.remaining == 240_000  # 300K - 10K spent - 50K reserved
    assert p.available_for_children == 240_000


def test_reserve_multiple_children():
    p = BudgetPlanner(max_budget=300_000)
    p.reserve_for_child(50_000)
    p.reserve_for_child(50_000)
    p.reserve_for_child(50_000)
    assert p.reserved_for_children == 150_000
    assert p.remaining == 150_000


def test_reserve_exceeds_available():
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(60_000)
    # remaining = 40K
    import pytest
    with pytest.raises(ValueError, match="Cannot reserve"):
        p.reserve_for_child(50_000)
    # Reservation should not have been applied
    assert p.reserved_for_children == 0


def test_release_child_reservation():
    p = BudgetPlanner(max_budget=300_000)
    p.reserve_for_child(80_000)
    assert p.remaining == 220_000

    p.release_child_reservation(80_000)
    assert p.reserved_for_children == 0
    assert p.remaining == 300_000


def test_release_partial():
    """Release less than total reservation."""
    p = BudgetPlanner(max_budget=300_000)
    p.reserve_for_child(50_000)
    p.reserve_for_child(50_000)
    p.release_child_reservation(30_000)
    assert p.reserved_for_children == 70_000


def test_release_more_than_reserved():
    """Release clamped to actual reserved amount."""
    p = BudgetPlanner(max_budget=300_000)
    p.reserve_for_child(20_000)
    p.release_child_reservation(100_000)
    assert p.reserved_for_children == 0


def test_reservation_affects_finalize():
    """Reservations reduce remaining, which should trigger finalize sooner."""
    p = BudgetPlanner(max_budget=100_000)
    p.record_call(10_000)
    p.record_call(10_000)
    # remaining = 80K, no finalize yet
    assert p.should_finalize() is False

    # Reserve 70K for children
    p.reserve_for_child(70_000)
    # remaining = 10K now
    # est_next = max(10K, 10K) * 1.2 = 12K
    # 10K < 12K + 10K => should finalize
    assert p.should_finalize() is True


def test_snapshot_includes_reservations():
    p = BudgetPlanner(max_budget=300_000)
    p.record_call(10_000)
    p.reserve_for_child(50_000)
    snap = p.snapshot()
    assert snap["reserved_for_children"] == 50_000
    assert snap["available_for_children"] == 240_000
    assert snap["remaining"] == 240_000


def test_small_uniform_calls_no_early_finalize():
    """Many small calls should use most of the budget."""
    p = BudgetPlanner(max_budget=100_000)
    for _ in range(15):
        p.record_call(3_000)
    # total = 45K, remaining = 55K
    # max of recent = 3K, est = 3K * 1.2 = 3600
    # 55K > 3600 + 10K => should NOT finalize
    assert p.should_finalize() is False

    for _ in range(12):
        p.record_call(3_000)
    # total = 81K, remaining = 19K
    # 19K > 3600 + 10K = 13600 => still NOT finalize
    assert p.should_finalize() is False

    for _ in range(3):
        p.record_call(3_000)
    # total = 90K, remaining = 10K
    # 10K < 3600 + 10K = 13600 => SHOULD finalize
    assert p.should_finalize() is True
    assert p.pct_used == 90.0
