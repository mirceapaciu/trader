"""Commission and slippage models for simulated order fills."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommissionModel:
    """Commission cost model for a simulated fill.

    ``per_share`` charges ``per_share_usd`` per (absolute) share with a
    ``min_usd`` floor; ``flat`` charges ``flat_usd`` regardless of size.
    """

    model: str = "per_share"
    per_share_usd: float = 0.005
    min_usd: float = 1.0
    flat_usd: float = 0.0

    def cost(self, quantity: float, price: float) -> float:
        """Return the commission for filling ``quantity`` at ``price``."""

        if self.model == "per_share":
            return max(self.min_usd, abs(quantity) * self.per_share_usd)
        if self.model == "flat":
            return self.flat_usd
        raise ValueError(f"Unknown commission model: {self.model!r}")


def apply_slippage(price: float, slippage_bps: float, *, worse_is_higher: bool) -> float:
    """Apply ``slippage_bps`` basis points of slippage to ``price``.

    When ``worse_is_higher`` the price is moved up (e.g. a buy entry); otherwise
    it is moved down (e.g. a sell exit).
    """

    bps_fraction = slippage_bps / 10_000
    if worse_is_higher:
        return price * (1 + bps_fraction)
    return price * (1 - bps_fraction)
