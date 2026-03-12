"""Budget planner: tracks per-call costs and decides when to finalize.

The planner observes actual API call costs and estimates whether the budget
can afford another tool iteration. When it cannot, it signals the LLM client
to make one final no-tools call instead of continuing the tool loop.
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Reserve enough for one final LLM response (no tools).
# Intentionally generous — better to under-use by 5% than over-use by 50%.
DEFAULT_FINALIZE_RESERVE_RATIO = 0.10  # 10% of budget

# Hard stop at 115% — absolute safety valve if finalize doesn't catch it.
HARD_STOP_RATIO = 1.15


@dataclass
class BudgetPlanner:
    """Tracks token spend and decides when the agent must finalize.

    The planner is created with a max_budget. After each API call, the
    caller reports the cost. The planner maintains a sliding window of
    recent per-call costs and uses the MAXIMUM of recent calls (conservative)
    to estimate the next call's cost.

    Key concept: "finalize reserve". The planner always reserves enough
    tokens for one final no-tools API call. When the remaining budget
    cannot cover both the estimated next tool call AND the finalize
    reserve, the planner signals that the agent should finalize now.
    """

    max_budget: int
    finalize_reserve_ratio: float = DEFAULT_FINALIZE_RESERVE_RATIO
    _call_costs: list[int] = field(default_factory=list)
    _total_spent: int = 0
    _finalize_triggered: bool = False
    _reserved_for_children: int = 0

    # Sliding window size for cost estimation
    WINDOW_SIZE: int = 5

    @property
    def total_spent(self) -> int:
        return self._total_spent

    @property
    def remaining(self) -> int:
        """Tokens remaining for this agent's own use (excludes child reservations)."""
        return max(0, self.max_budget - self._total_spent - self._reserved_for_children)

    @property
    def reserved_for_children(self) -> int:
        return self._reserved_for_children

    @property
    def available_for_children(self) -> int:
        """How many tokens can still be allocated to new children."""
        return self.remaining

    @property
    def pct_used(self) -> float:
        if self.max_budget <= 0:
            return 0.0
        return (self._total_spent / self.max_budget) * 100

    @property
    def finalize_reserve(self) -> int:
        """Absolute token count reserved for the final response."""
        return int(self.max_budget * self.finalize_reserve_ratio)

    @property
    def finalize_triggered(self) -> bool:
        return self._finalize_triggered

    def reserve_for_child(self, amount: int) -> None:
        """Reserve tokens from this budget for a child agent.

        Raises ValueError if insufficient budget remains.
        """
        if amount <= 0:
            raise ValueError("Reservation amount must be positive")
        if amount > self.available_for_children:
            raise ValueError(
                f"Cannot reserve {amount:,} tokens — only {self.available_for_children:,} available "
                f"(budget={self.max_budget:,}, spent={self._total_spent:,}, "
                f"already_reserved={self._reserved_for_children:,})"
            )
        self._reserved_for_children += amount
        logger.info(
            f"BudgetPlanner: reserved {amount:,} for child. "
            f"Total reserved={self._reserved_for_children:,}, remaining={self.remaining:,}"
        )

    def release_child_reservation(self, amount: int) -> None:
        """Release tokens back when a child finishes under budget."""
        released = min(amount, self._reserved_for_children)
        self._reserved_for_children -= released
        if released > 0:
            logger.info(
                f"BudgetPlanner: released {released:,} from child reservation. "
                f"Total reserved={self._reserved_for_children:,}, remaining={self.remaining:,}"
            )

    def record_call(self, total_tokens: int) -> None:
        """Record the cost of one API call.

        Args:
            total_tokens: The total_tokens from this API response
                (prompt + completion for this single call).
        """
        self._call_costs.append(total_tokens)
        self._total_spent += total_tokens

    def estimated_next_cost(self) -> int:
        """Estimate the cost of the next API call.

        Uses the MAX of the most recent calls (conservative).
        Conversation growth means later calls are more expensive,
        so the max of recent calls with a growth factor is a
        reasonable estimate for the next call.
        """
        if not self._call_costs:
            # No data yet — cannot estimate. Allow the call.
            return 0

        recent = self._call_costs[-self.WINDOW_SIZE:]

        if len(recent) == 1:
            # Only one call observed. Next will be bigger due to
            # conversation growth. Use 1.5x.
            return int(recent[0] * 1.5)

        # Max of recent calls + 20% growth factor
        max_recent = max(recent)
        return int(max_recent * 1.2)

    def should_finalize(self) -> bool:
        """Check whether the budget can afford another tool iteration.

        Returns True if remaining budget cannot cover:
          estimated_next_cost + effective_reserve

        The effective reserve is max(finalize_reserve, estimated_next_cost),
        because the finalize call (no tools, full conversation context)
        costs roughly the same as a regular call in prompt tokens.

        Once triggered, stays True (sticky).
        """
        if self._finalize_triggered:
            return True

        est = self.estimated_next_cost()
        # Finalize call costs at least as much as a regular call
        # (same conversation context in prompt), so use the larger of
        # the fixed reserve and the estimated cost.
        reserve = max(self.finalize_reserve, est)

        can_afford = self.remaining > (est + reserve)

        if not can_afford:
            self._finalize_triggered = True
            logger.info(
                f"BudgetPlanner: finalize triggered. "
                f"Spent={self._total_spent:,}, remaining={self.remaining:,}, "
                f"est_next={est:,}, reserve={reserve:,}"
            )

        return not can_afford

    def hard_stop(self) -> bool:
        """Absolute safety valve: has spending exceeded 115% of budget?

        The finalize mechanism should prevent reaching this point, but
        if it fails (e.g. the finalize call itself is expensive), this
        stops everything.
        """
        return self._total_spent > int(self.max_budget * HARD_STOP_RATIO)

    def snapshot(self) -> dict:
        """Return planner state for diagnostics (used by check_budget tool)."""
        est = self.estimated_next_cost()
        return {
            "total_spent": self._total_spent,
            "max_budget": self.max_budget,
            "remaining": self.remaining,
            "reserved_for_children": self._reserved_for_children,
            "available_for_children": self.available_for_children,
            "pct_used": round(self.pct_used, 1),
            "estimated_next_cost": est,
            "finalize_reserve": self.finalize_reserve,
            "finalize_triggered": self._finalize_triggered,
            "calls_recorded": len(self._call_costs),
            "estimated_calls_remaining": (
                max(0, (self.remaining - self.finalize_reserve) // max(1, est))
                if est > 0 else None
            ),
            "recent_call_costs": self._call_costs[-self.WINDOW_SIZE:],
        }
