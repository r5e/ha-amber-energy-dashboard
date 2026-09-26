"""Synthetic Amber usage records, in the exact shape the API returns.

All values are invented. Record structure, field order, the one-second ``startTime``
offset and the ``cost = round(kwh * perKwh, 4)`` rule follow the recorded responses
(see ``fixtures/usage_2026-09-24.json``).
"""

from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta, timezone
import math
import random
from typing import Any

NEM = timezone(timedelta(hours=10))
SITE_ID = "01FAKESITE0000000000000000"
DEFAULT_CHANNELS = (("E1", "general", "EA116"), ("B1", "feedIn", None))


def _z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _price(channel_type: str, local_hour: float, rng: random.Random) -> float:
    if channel_type == "feedIn":
        base = -8.0 if 10 <= local_hour < 15 else -3.0
    elif channel_type == "controlledLoad":
        base = 14.0
    else:
        base = 38.0 if 15 <= local_hour < 21 else 24.0
    return round(base * rng.uniform(0.9, 1.1), 5)


def _kwh(channel_type: str, local_hour: float, duration: int, rng: random.Random) -> float:
    solar = 4.0 * max(0.0, math.sin(math.pi * (local_hour - 6.5) / 11.0))
    load = 0.4 + 1.5 * math.exp(-(((local_hour - 19.0) / 1.5) ** 2))
    if channel_type == "feedIn":
        kw = max(solar - load, 0.0)
    elif channel_type == "controlledLoad":
        kw = 2.4 if 0 <= local_hour < 3 else 0.0
    else:
        kw = max(load - solar, 0.0) * rng.uniform(0.8, 1.2)
    return round(kw * duration / 60, 3)


def make_usage_day(
    day: date,
    channels: Iterable[tuple[str, str, str | None]] = DEFAULT_CHANNELS,
    *,
    duration: int = 5,
    quality: str = "billable",
    seed: int = 0,
) -> list[dict[str, Any]]:
    """Return one complete NEM day of usage records for the given channels."""
    rng = random.Random(day.toordinal() * 1000 + seed)
    day_start = datetime.combine(day, time(0), NEM)
    records = []
    for ident, channel_type, _tariff in channels:
        for i in range(1440 // duration):
            start = day_start + timedelta(minutes=i * duration)
            end = start + timedelta(minutes=duration)
            local_hour = (i * duration + duration / 2) / 60
            kwh = _kwh(channel_type, local_hour, duration, rng)
            per_kwh = _price(channel_type, local_hour, rng)
            cost = round(kwh * per_kwh, 4)
            record: dict[str, Any] = {
                "type": "Usage",
                "duration": duration,
                "date": day.isoformat(),
                "endTime": _z(end),
                "quality": quality,
                "kwh": kwh if kwh else 0,
                "nemTime": end.isoformat(),
                "perKwh": per_kwh,
                "channelType": channel_type,
                "channelIdentifier": ident,
                "cost": cost if cost else 0,
                "renewables": round(rng.uniform(10, 60), 3),
                "spotPerKwh": round(abs(per_kwh) * 0.5, 5),
                "startTime": _z(start + timedelta(seconds=1)),
                "spikeStatus": "none",
                "descriptor": "neutral",
            }
            if channel_type != "feedIn":
                record["tariffInformation"] = {"period": "offPeak", "season": "nonSummer"}
            records.append(record)
    return records


def site_json(channels: Iterable[tuple[str, str, str | None]] = DEFAULT_CHANNELS, **extra: Any):
    """Return a /sites element for the fake site."""
    site = {
        "id": SITE_ID,
        "nmi": "FAKENMI000",
        "channels": [
            {"identifier": i, "type": t, **({"tariff": tf} if tf else {})} for i, t, tf in channels
        ],
        "network": "Example Network",
        "status": "active",
        "activeFrom": "2025-01-01",
        "intervalLength": 5,
    }
    site.update(extra)
    return site


def expected_totals(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Independent per-channel day totals: kWh and cost in cents (for assertions)."""
    totals: dict[str, dict[str, list[float]]] = {}
    for r in records:
        t = totals.setdefault(r["channelIdentifier"], {"kwh": [], "cents": []})
        t["kwh"].append(r["kwh"])
        t["cents"].append(r["cost"])
    return {
        k: {"kwh": math.fsum(v["kwh"]), "cents": math.fsum(v["cents"])} for k, v in totals.items()
    }
