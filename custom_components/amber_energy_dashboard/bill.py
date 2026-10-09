"""Bill estimate (DESIGN section 19).

A billing cycle runs from the billing day of one month to the day before it the next
month. Cycle dates are **NEM days** (00:00 to 24:00 AEST, all year), the same days
Amber's meter data and bill use: during daylight saving a cycle starts at 01:00 local
time, and the local hour 00:00 to 01:00 on the billing day belongs to the previous cycle.

Only **complete imported days** count: a day is in the estimate once its usage is
imported (every interval, verified). Daily fixed charges are entered excluding GST and
apply to every day from the first to the last imported day of the cycle; usage (Amber's
values) already includes GST, and export credits carry none. One-off charges (for
example card payment fees) are out of scope.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
import math
from typing import Any, Final

from .const import CHANNEL_FEED_IN
from .statistics import ChannelConfig, Metric, StatisticSpec

CHARGES: Final = ("daily_supply", "amber_subscription", "other_daily")
"""The named daily fixed charges, in AUD per day excluding GST, as on the Amber bill's
charges summary: the network daily supply charge (which includes metering), Amber's
subscription, and any other daily charge."""
LEGACY_CHARGES: Final = {
    "network_daily": "daily_supply",
    "metering_daily": "daily_supply",
    "subscription_daily": "amber_subscription",
}
"""2.1.0-rc1's charge options, and the option each one is added into (2.1.0-rc2)."""


def migrate_charges(options: Mapping[str, Any]) -> dict[str, Any]:
    """Options with rc1's charges folded into the rc2 fields: network + metering = daily
    supply, subscription = Amber subscription; other daily charges are kept."""
    out = {k: v for k, v in options.items() if k not in LEGACY_CHARGES}
    for old, new in LEGACY_CHARGES.items():
        if old in options:
            out[new] = round(float(out.get(new) or 0.0) + float(options[old] or 0.0), 6)
    return out


DEFAULT_GST_PERCENT: Final = 10.0
MAX_BILLING_DAY: Final = 28
_CENTS: Final = 2
_DETAIL: Final = 4


@dataclass(frozen=True, slots=True)
class BillSettings:
    """The bill estimate options."""

    billing_day: int
    charges: Mapping[str, float]
    gst_percent: float = DEFAULT_GST_PERCENT
    fixed_statistic: bool = False

    @property
    def gst(self) -> float:
        """The GST rate as a fraction."""
        return self.gst_percent / 100

    @property
    def daily_fixed_incl_gst(self) -> float:
        """All daily fixed charges, including GST."""
        return math.fsum(self.charges.get(name, 0.0) for name in CHARGES) * (1 + self.gst)


def cycle_for(day: date, billing_day: int) -> tuple[date, date]:
    """The billing cycle (first and last day) that contains ``day``."""
    if day.day >= billing_day:
        start = day.replace(day=billing_day)
    else:
        start = (day.replace(day=1) - timedelta(days=1)).replace(day=billing_day)
    following = (start.replace(day=1) + timedelta(days=32)).replace(day=billing_day)
    return start, following - timedelta(days=1)


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def estimate(
    settings: BillSettings,
    days: Mapping[date, Mapping[str, Any] | None],
    specs: list[StatisticSpec],
    channels: tuple[ChannelConfig, ...],
    *,
    cycle_day: date,
    today: date,
    export: Mapping[date, tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """The estimate for the cycle containing ``cycle_day``, from the Store's day records.

    ``days`` maps each date of the cycle to its Store record (or None). ``today`` is the
    current NEM date (for "days into cycle"). ``export`` (with the export allowance,
    section 20) maps days to (penalty, refund) in AUD: the export credit is then shown
    before the network's export charge, which gets its own line after the allowance.
    """
    start, end = cycle_for(cycle_day, settings.billing_day)
    feed_in = {c.identifier for c in channels if c.type == CHANNEL_FEED_IN}
    cost_ids = [s.statistic_id for s in specs if s.metric is Metric.COST]
    compensation_ids = [
        s.statistic_id for s in specs if s.metric is Metric.COMPENSATION and s.channel in feed_in
    ]
    imported = [
        day for day in _days(start, end) if (days.get(day) or {}).get("status") == "imported"
    ]
    in_cycle = len(_days(start, end))
    result: dict[str, Any] = {
        "cycle_start": start.isoformat(),
        "cycle_end": end.isoformat(),
        "days_in_cycle": in_cycle,
        "days_into_cycle": min(max((today - start).days + 1, 0), in_cycle),
        "gst_percent": settings.gst_percent,
        "data_from": None,
        "data_through": None,
        "days_elapsed": 0,
        "days_imported": len(imported),
        "missing_days": [],
        "complete_from_cycle_start": False,
        "lines": {},
        "bill_to_date": None,
        "projected_bill": None,
        "average_cost_per_day": None,
    }
    if not imported:
        return result
    first, last = imported[0], imported[-1]
    covered = _days(first, last)
    usage = math.fsum(
        (days[day] or {}).get("totals", {}).get(sid, 0.0) for day in imported for sid in cost_ids
    )
    credit = math.fsum(
        (days[day] or {}).get("totals", {}).get(sid, 0.0)
        for day in imported
        for sid in compensation_ids
    )
    penalty = refund = 0.0
    if export is not None:
        penalty = math.fsum(export[d][0] for d in imported if d in export)
        refund = math.fsum(export[d][1] for d in imported if d in export)
    multiplier = 1 + settings.gst
    fixed = {
        name: settings.charges.get(name, 0.0) * len(covered) * multiplier
        for name in CHARGES
        if settings.charges.get(name)
    }
    credit += refund  # the adjusted compensation
    bill_to_date = usage - credit + math.fsum(fixed.values())
    per_usage_day = (usage - credit) / len(imported)
    export_lines = (
        {
            "export_credit": round(-(credit + penalty - refund), _DETAIL),
            "network_export_charge": round(penalty - refund, _DETAIL),
        }
        if export is not None
        else {"export_credit": round(-credit, _DETAIL)}
    )
    result.update(
        {
            "data_from": first.isoformat(),
            "data_through": last.isoformat(),
            "days_elapsed": len(covered),
            "missing_days": [d.isoformat() for d in covered if d not in imported],
            "complete_from_cycle_start": first == start,
            "lines": {
                "usage": round(usage, _DETAIL),
                **export_lines,
                **{name: round(value, _DETAIL) for name, value in fixed.items()},
            },
            "bill_to_date": round(bill_to_date, _CENTS),
            "projected_bill": round(
                per_usage_day * in_cycle + settings.daily_fixed_incl_gst * in_cycle, _CENTS
            ),
            "average_cost_per_day": round(bill_to_date / len(covered), _CENTS),
        }
    )
    return result
