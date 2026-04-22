import random
from dataclasses import dataclass


@dataclass(slots=True)
class VariantStats:
    variant_label: str
    sent: int
    unique_clicks: int
    unique_opens: int
    bounces: int
    complaints: int


def select_variant(
    stats: list[VariantStats],
    min_explore_sends: int = 200,
) -> str | None:
    if not stats:
        return None
    underexplored = [s for s in stats if s.sent < min_explore_sends]
    if underexplored:
        return random.choice(underexplored).variant_label
    samples = []
    for s in stats:
        alpha = 1 + s.unique_clicks
        beta = 1 + max(0, s.sent - s.unique_clicks)
        samples.append((random.betavariate(alpha, beta), s.variant_label))
    return max(samples, key=lambda x: x[0])[1]


@dataclass(slots=True)
class RetirementDecision:
    variant_label: str
    retire: bool
    reason: str | None


def decide_retirement(
    stats: list[VariantStats],
    min_retire_sends: int,
    max_bounce_rate: float = 0.02,
    max_complaint_rate: float = 0.001,
) -> list[RetirementDecision]:
    decisions: list[RetirementDecision] = []
    eligible = [s for s in stats if s.sent >= min_retire_sends]
    best_click_rate = (
        max((s.unique_clicks / s.sent) for s in eligible) if eligible else 0.0
    )
    for s in stats:
        if s.sent < min_retire_sends:
            decisions.append(RetirementDecision(s.variant_label, False, None))
            continue
        if s.bounces / s.sent > max_bounce_rate:
            decisions.append(RetirementDecision(s.variant_label, True, "bounce_rate"))
            continue
        if s.complaints / s.sent > max_complaint_rate:
            decisions.append(RetirementDecision(s.variant_label, True, "complaint_rate"))
            continue
        click_rate = s.unique_clicks / s.sent
        if (
            best_click_rate > 0
            and click_rate < 0.5 * best_click_rate
            and s.sent >= 2 * min_retire_sends
        ):
            decisions.append(
                RetirementDecision(s.variant_label, True, "click_rate_vs_best")
            )
            continue
        decisions.append(RetirementDecision(s.variant_label, False, None))
    return decisions
