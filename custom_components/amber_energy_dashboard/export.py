"""Free export allowance on two-way network tariffs (DESIGN section 20).

Amber's feed-in ``perKwh`` (negative when earned) is, per interval::

    earned rate = loss factor x spot + network component

The network component is a charge in the penalty period (Endeavour N61: -1.86 c/kWh in
``solarSponge``, 10:00 to 14:00) and a reward in peak (+3.47 c/kWh). The period of an
interval comes from the general channel's ``tariffInformation.period`` for the same
interval (feed-in records have none), never from the clock.

Amber's usage data deducts the penalty from every interval. The network's free allowance
(N61: 2,920 kWh a year, "calculated on a daily basis and applied to the billing period")
is applied on the bill instead: allowance = kWh per day x days in the billing period,
against the total export in the penalty window over the period; only the excess is
charged. So the penalty on window exports within the allowance is refunded, in time
order, until the cycle's window export passes the allowance.

The rates are measured from the data, not taken from the network's price list (Amber
applies 1.86 c where Endeavour publishes 1.79 c): the loss factor is the median of
earned rate ÷ spot over off-period intervals with a spot price of at least 5 c/kWh, and
the component is earned rate - loss factor x spot.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
import math
from statistics import median
from typing import Any, Final

from .api import UsageRecord
from .bill import cycle_for
from .importer import DAY_HOURS, HOUR, floor_minute, nem_day_start, revision_fingerprint

TOTALLING_PERIOD: Final = "billing_period"
TOTALLING_DAILY: Final = "daily"
TOTALLINGS: Final = (TOTALLING_PERIOD, TOTALLING_DAILY)
DEFAULT_PENALTY_PERIOD: Final = "solarSponge"
DEFAULT_REWARD_PERIOD: Final = "peak"
MIN_SPOT_FOR_LOSS_FACTOR: Final = 5.0
"""c/kWh: off-period intervals with a smaller spot price don't measure the loss factor."""
RECENT_DAYS: Final = 30


@dataclass(frozen=True, slots=True)
class TwoWayTariff:
    """A verified two-way network tariff with a free export allowance."""

    network: str
    tariff: str
    allowance_kwh_per_day: float
    penalty_period: str
    reward_period: str
    totalling: str
    source: str


KNOWN_TARIFFS: Final = (
    TwoWayTariff(
        network="Endeavour Energy",
        tariff="N61",
        allowance_kwh_per_day=8.0,
        penalty_period="solarSponge",
        reward_period="peak",
        totalling=TOTALLING_PERIOD,
        source="AER 2025-26 pricing proposal (2,920 kWh a year, daily, applied to the "
        "billing period); verified against the September 2026 bill",
    ),
)


def detect_tariff(network: str | None, tariffs: Sequence[str | None]) -> TwoWayTariff | None:
    """The known two-way tariff for a site's network and channel tariff codes, if any."""
    for known in KNOWN_TARIFFS:
        if (network or "").casefold() == known.network.casefold() and known.tariff in tariffs:
            return known
    return None


@dataclass(frozen=True, slots=True)
class AllowanceSettings:
    """How the allowance is applied."""

    allowance_kwh_per_day: float
    totalling: str
    penalty_period: str
    reward_period: str
    billing_day: int | None
    """Needed for billing-period totalling."""

    def period_for(self, day: date) -> tuple[date, date]:
        """The allowance period containing ``day``: its billing cycle, or the day itself."""
        if self.totalling == TOTALLING_DAILY or self.billing_day is None:
            return day, day
        return cycle_for(day, self.billing_day)


def day_aggregates(
    records: Sequence[UsageRecord],
    day: date,
    settings: AllowanceSettings,
    *,
    channels: tuple[str, str],
    fallback_loss_factor: float | None,
) -> dict[str, Any] | None:
    """One day's export aggregates, or None when the day has no feed-in records.

    ``window_kwh_hours`` is the window export per NEM hour; the rates are in c/kWh, the
    amounts in AUD (penalty: charged by Amber in the window; reward: earned in peak).
    ``channels`` are the general and feed-in channel identifiers.
    """
    general, feed_in = channels
    periods = {
        floor_minute(r.start_time): (r.tariff_information or {}).get("period")
        for r in records
        if r.channel_identifier == general
    }
    feed = [r for r in records if r.channel_identifier == feed_in]
    if not feed:
        return None
    special = (settings.penalty_period, settings.reward_period)
    ratios = [
        -r.per_kwh / r.spot_per_kwh
        for r in feed
        if periods.get(floor_minute(r.start_time)) not in special
        and r.spot_per_kwh is not None
        and abs(r.spot_per_kwh) >= MIN_SPOT_FOR_LOSS_FACTOR
    ]
    loss_factor = median(ratios) if ratios else fallback_loss_factor
    day_start = nem_day_start(day)
    hours = [0.0] * DAY_HOURS
    reward_kwh = 0.0
    penalty_components: list[float] = []
    reward_components: list[float] = []
    for r in feed:
        start = floor_minute(r.start_time)
        period = periods.get(start)
        component = (
            -r.per_kwh - loss_factor * r.spot_per_kwh
            if loss_factor is not None and r.spot_per_kwh is not None
            else None
        )
        if period == settings.penalty_period:
            hours[int((start - day_start) / HOUR)] += r.kwh
            if component is not None:
                penalty_components.append(-component)
        elif period == settings.reward_period:
            reward_kwh += r.kwh
            if component is not None:
                reward_components.append(component)
    window = math.fsum(hours)
    penalty_rate = median(penalty_components) if penalty_components else None
    reward_rate = median(reward_components) if reward_components else None
    return {
        "window_kwh": round(window, 4),
        "window_kwh_hours": [round(h, 4) for h in hours],
        "penalty_rate": None if penalty_rate is None else round(penalty_rate, 4),
        "reward_kwh": round(reward_kwh, 4),
        "reward_rate": None if reward_rate is None else round(reward_rate, 4),
        "loss_factor": None if loss_factor is None else round(loss_factor, 6),
        "penalty": round(window * (penalty_rate or 0.0) / 100, 6),
        "reward": round(reward_kwh * (reward_rate or 0.0) / 100, 6),
        "measured": loss_factor is not None,
        "fingerprint": revision_fingerprint(records),
    }


def refunds(
    settings: AllowanceSettings, days: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[int, float], dict[str, dict[str, Any]]]:
    """The penalty refunded within the allowance, per hour (UTC timestamp → AUD), and a
    summary per allowance period (keyed by its first day).

    Within a period, window exports are set against the allowance in time order; the
    penalty on the part within it is refunded, and the rest is the export charge. Days
    without aggregates count nothing (their window export is unknown).
    """
    by_hour: dict[int, float] = {}
    periods: dict[str, dict[str, Any]] = {}
    for key in sorted(days):
        record = days[key]
        day = date.fromisoformat(key)
        first, last = settings.period_for(day)
        summary = periods.setdefault(
            first.isoformat(),
            {
                "start": first.isoformat(),
                "end": last.isoformat(),
                "allowance_kwh": settings.allowance_kwh_per_day * ((last - first).days + 1),
                "window_kwh": 0.0,
                "penalty": 0.0,
                "refund": 0.0,
                "days": [],
            },
        )
        rate = record.get("penalty_rate") or 0.0
        day_start = nem_day_start(day)
        for hour, kwh in enumerate(record["window_kwh_hours"]):
            remaining = max(summary["allowance_kwh"] - summary["window_kwh"], 0.0)
            refunded = min(kwh, remaining) * rate / 100
            summary["window_kwh"] += kwh
            if refunded:
                ts = round((day_start + hour * HOUR).timestamp())
                by_hour[ts] = refunded
                summary["refund"] += refunded
        summary["penalty"] += record["penalty"]
        summary["days"].append(key)
    return by_hour, periods


def period_summary(
    settings: AllowanceSettings,
    days: Mapping[str, Mapping[str, Any]],
    period_day: date,
    imported: Sequence[date],
) -> dict[str, Any]:
    """The allowance figures for the period containing ``period_day``.

    ``imported`` are the period's imported days; those without aggregates are listed as
    unadjusted.
    """
    first, last = settings.period_for(period_day)
    inside = {k: v for k, v in days.items() if first.isoformat() <= k <= last.isoformat()}
    _, periods = refunds(settings, inside)
    summary = periods.get(first.isoformat()) or {
        "allowance_kwh": settings.allowance_kwh_per_day * ((last - first).days + 1),
        "window_kwh": 0.0,
        "penalty": 0.0,
        "refund": 0.0,
        "days": [],
    }
    recent = sorted(days)[-RECENT_DAYS:]
    rates = [days[k]["penalty_rate"] for k in recent if days[k].get("penalty_rate") is not None]
    rewards = [days[k]["reward_rate"] for k in recent if days[k].get("reward_rate") is not None]
    used = min(summary["window_kwh"], summary["allowance_kwh"])
    return {
        "period_start": first.isoformat(),
        "period_end": last.isoformat(),
        "totalling": settings.totalling,
        "penalty_period": settings.penalty_period,
        "allowance_kwh": round(summary["allowance_kwh"], 3),
        "window_export_kwh": round(summary["window_kwh"], 3),
        "allowance_used_kwh": round(used, 3),
        "allowance_remaining_kwh": round(summary["allowance_kwh"] - used, 3),
        "penalty": round(summary["penalty"], 4),
        "refund": round(summary["refund"], 4),
        "export_charge": round(summary["penalty"] - summary["refund"], 4),
        "data_through": max(summary["days"]) if summary["days"] else None,
        "unadjusted_days": [d.isoformat() for d in imported if d.isoformat() not in inside],
        "penalty_rate_c": round(median(rates), 4) if rates else None,
        "reward_rate_c": round(median(rewards), 4) if rewards else None,
    }


def day_refunds(
    settings: AllowanceSettings, days: Mapping[str, Mapping[str, Any]], first: date, last: date
) -> dict[date, tuple[float, float]]:
    """(penalty, refund) in AUD for each day from ``first`` to ``last`` that has
    aggregates; the refunds follow the allowance periods of the whole history."""
    by_hour, _ = refunds(settings, days)
    out: dict[date, tuple[float, float]] = {}
    day = first
    while day <= last:
        record = days.get(day.isoformat())
        if record is not None:
            start = nem_day_start(day)
            refund = math.fsum(
                by_hour.get(round((start + h * HOUR).timestamp()), 0.0) for h in range(DAY_HOURS)
            )
            out[day] = (record["penalty"], refund)
        day += timedelta(days=1)
    return out
