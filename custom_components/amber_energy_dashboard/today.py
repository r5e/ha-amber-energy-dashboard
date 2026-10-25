"""Today so far (DESIGN section 22, 2.2.0-dev3): today's import cost and feed-in credit,
estimated live from a grid meter and the live price. Display only: never written into
long-term statistics.

Every 5 minutes (on the NEM interval boundaries) each side's energy since the previous
boundary is read from the grid meter's mapped sensor (import, and export when the meter
has one) and priced at the live price that was in effect at the end of the interval
(the last price state before the boundary). Days are NEM days, as Amber bills them.

- A sensor that is unavailable at a boundary contributes nothing then; its energy counts
  at the next boundary it reports, priced at that interval's price.
- Energy with no usable price (the price sensor unavailable, or in an unknown unit) is
  counted as ``unpriced_kwh``, not priced.
- A sensor reading that goes down (a meter reset) drops that interval.
- After a restart within the same day, the energy used while Home Assistant was down is
  priced at the first interval's price. Across midnight it is dropped (which day it
  belongs to is unknown).

**Calibration offset** (2.2.0): the estimate applies the grid meter's calibration offset
(the offset, in W, times the interval's length, added to the import while the interval
imports more than it exports, else taken from the export). An interval without a known
offset counts as measured. Each side also keeps the raw estimate (the meter's energy as
measured), for comparison: in production the corrected estimate was the closer one.

The state is kept in a small Store of its own (saved at most every 30 s, and when Home
Assistant stops), with each finished day's estimate for 14 days, so it can be compared
with the billed day once Amber's data arrives.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import math
from typing import Any, Final

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfEnergy
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event, async_track_utc_time_change
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_conversion import EnergyConverter

from .const import DOMAIN, NEM_TZ
from .importer import nem_day_start
from .sources import price_from_state

INTERVAL: Final = timedelta(minutes=5)
INTERVALS_PER_DAY: Final = 288
HISTORY_DAYS: Final = 14
SAVE_DELAY: Final = 30
STORE_VERSION: Final = 1


def store_key(site_key: str) -> str:
    """The tracker's Store, next to the site's."""
    return f"{DOMAIN}.{site_key}_today"


def interval_end(moment: datetime) -> datetime:
    """The interval boundary at or before ``moment`` (UTC)."""
    moment = moment.astimezone(UTC)
    return moment.replace(minute=moment.minute - moment.minute % 5, second=0, microsecond=0)


def nem_date(moment: datetime) -> date:
    """The NEM day ``moment`` falls in."""
    return moment.astimezone(NEM_TZ).date()


@dataclass(frozen=True, slots=True)
class Side:
    """One side of the grid meter: its energy sensor and the live price it is priced at."""

    role: str
    """import or export."""
    energy_entity: str
    price_entity: str
    channel: str
    """The Amber channel it is billed on (its identifier), for the comparison."""


_TOTALS: Final = (
    "kwh",
    "amount",
    "unpriced_kwh",
    "intervals",
    "estimate_kwh",
    "estimate_amount",
    "corrected_intervals",
)
"""Each side's day totals. kwh and amount are raw (as measured); estimate_kwh and
estimate_amount have the calibration offset applied in the corrected_intervals that had
one (raw in the others)."""


def _empty_side(entity_id: str | None = None) -> dict[str, Any]:
    return {
        "kwh": 0.0,
        "amount": 0.0,
        "unpriced_kwh": 0.0,
        "intervals": 0,
        "estimate_kwh": 0.0,
        "estimate_amount": 0.0,
        "corrected_intervals": 0,
        "reading": None,
        "entity_id": entity_id,
    }


def upgraded(record: Mapping[str, Any]) -> dict[str, Any]:
    """A side's totals as saved by 2.2.0rc1 or earlier, with the estimate. Those kept the
    offset-corrected totals of only the intervals that had an offset (none before
    2.2.0b3): the other intervals are added at the raw rate, in proportion."""
    result = {k: v for k, v in record.items() if k not in ("corrected_kwh", "corrected_amount")}
    if "estimate_amount" in record:
        return result
    intervals = record.get("intervals", 0)
    corrected = min(record.get("corrected_intervals", 0), intervals)
    rest = (intervals - corrected) / intervals if intervals else 1.0
    for total, raw in (("estimate_kwh", "kwh"), ("estimate_amount", "amount")):
        part = record.get(f"corrected_{raw}", 0.0) if corrected else 0.0
        result[total] = part + record.get(raw, 0.0) * rest
    result["corrected_intervals"] = corrected
    return result


def offset_corrected(deltas: Mapping[str, float], correction: float) -> dict[str, float]:
    """Each side's energy with ``correction`` (kWh, the calibration offset over the
    interval) applied: added to the import when it is at least the export, else taken
    from the export. A side that would go below zero passes the rest to the other side
    (when that side has a reading), so the net always changes by exactly the correction."""
    imp, exp = deltas.get("import"), deltas.get("export")
    if exp is None:
        return {} if imp is None else {"import": max(imp + correction, 0.0)}
    if imp is None:
        return {"export": max(exp - correction, 0.0)}
    if imp >= exp:
        imp += correction
    else:
        exp -= correction
    if imp < 0:
        exp, imp = exp - imp, 0.0
    if exp < 0:
        imp, exp = imp - exp, 0.0
    return {"import": imp, "export": exp}


class TodayTracker:
    """A site's Today so far."""

    def __init__(
        self,
        hass: HomeAssistant,
        site_key: str,
        sides: list[Side],
        meter_name: str,
        on_update: Callable[[], None],
        *,
        offset_w: Callable[[], float | None] = lambda: None,
    ) -> None:
        """Create the tracker (call async_start to begin). ``offset_w`` gives the grid
        meter's current calibration offset (W), or None."""
        self.hass = hass
        self._offset_w = offset_w
        self.sides = {side.role: side for side in sides}
        self.meter_name = meter_name
        self._on_update = on_update
        self._store: Store[dict[str, Any]] = Store(
            hass, STORE_VERSION, store_key(site_key), atomic_writes=True
        )
        self._data: dict[str, Any] = {"day": None, "last_tick": None, "sides": {}, "history": {}}
        self._prices: dict[str, list[tuple[float, float | None]]] = {r: [] for r in self.sides}
        """Each side's price samples since the last boundary: (time, $/kWh or None)."""
        self._unsubs: list[CALLBACK_TYPE] = []
        self._removed = False

    @property
    def history(self) -> dict[str, dict[str, Any]]:
        """Each finished day's estimate (ISO date -> {import, export}), at most 14 days."""
        return self._data["history"]

    async def async_load(self) -> None:
        """Load the saved state; a side whose sensor changed starts afresh."""
        data = await self._store.async_load() or {}
        self._data = {
            "day": data.get("day"),
            "last_tick": data.get("last_tick"),
            "sides": dict(data.get("sides") or {}),
            "history": dict(data.get("history") or {}),
        }
        for role, side in self.sides.items():
            saved = self._data["sides"].get(role)
            saved = None if saved is None else upgraded(saved)
            if saved is None or saved.get("entity_id") != side.energy_entity:
                fresh = _empty_side(side.energy_entity)
                if saved is not None:  # keep the day's totals, but don't diff readings
                    fresh.update({k: v for k, v in saved.items() if k in _TOTALS})
                self._data["sides"][role] = fresh
            else:
                self._data["sides"][role] = {**_empty_side(side.energy_entity), **saved}

    @callback
    def async_start(self) -> None:
        """Follow the price sensors and tick on each 5-minute boundary."""
        for role, side in self.sides.items():
            state = self.hass.states.get(side.price_entity)
            if state is not None:
                self._prices[role].append((state.last_updated.timestamp(), price_from_state(state)))
        by_price: dict[str, list[str]] = {}
        for role, side in self.sides.items():
            by_price.setdefault(side.price_entity, []).append(role)

        @callback
        def _price_changed(event: Event[EventStateChangedData]) -> None:
            state = event.data["new_state"]
            if state is None:
                return
            for role in by_price.get(event.data["entity_id"], []):
                self._prices[role].append((state.last_updated.timestamp(), price_from_state(state)))

        self._unsubs.append(
            async_track_state_change_event(self.hass, list(by_price), _price_changed)
        )

        @callback
        def _tick(now: datetime) -> None:
            self.async_tick(now)

        self._unsubs.append(
            async_track_utc_time_change(self.hass, _tick, minute=range(0, 60, 5), second=0)
        )

    @callback
    def async_stop(self) -> None:
        """Stop following; the state is saved when Home Assistant stops."""
        while self._unsubs:
            self._unsubs.pop()()

    async def async_save(self) -> None:
        """Save the state now (before another tracker takes over the Store)."""
        await self._store.async_save(self._data)

    async def async_shutdown(self) -> None:
        """Stop, and save now unless the site was removed: on an unload or a reload the
        next tracker loads the state (the delayed save would come too late)."""
        self.async_stop()
        if not self._removed:
            await self.async_save()

    async def async_remove(self) -> None:
        """Delete the saved state (the site was removed)."""
        self._removed = True
        await self._store.async_remove()

    def _reading(self, entity_id: str) -> float | None:
        """The sensor's reading in kWh, or None when it has none."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            return EnergyConverter.convert(
                float(state.state),
                state.attributes.get("unit_of_measurement"),
                UnitOfEnergy.KILO_WATT_HOUR,
            )
        except ValueError, HomeAssistantError:
            return None

    def _price_for(self, role: str, end: datetime) -> float | None:
        """The price in effect at the end of the interval ending at ``end``: the last
        sample before it (else the first after it, at startup). Older samples are dropped."""
        samples = self._prices[role]
        if not samples:
            return None
        before = [i for i, (at, _) in enumerate(samples) if at < end.timestamp()]
        index = before[-1] if before else 0
        del samples[:index]
        return samples[0][1]

    @callback
    def async_tick(self, now: datetime) -> None:
        """Price the interval ending at the boundary at or before ``now``."""
        end = interval_end(now)
        start = end - INTERVAL
        day = nem_date(start).isoformat()
        last_tick = self._data["last_tick"]
        same_day = self._data["day"] == day
        if not same_day:
            self._roll(day, contiguous=last_tick == start.isoformat())
        deltas: dict[str, float] = {}
        for role, side in self.sides.items():
            record = self._data["sides"][role]
            reading = self._reading(side.energy_entity)
            if reading is None:
                continue
            previous, record["reading"] = record["reading"], reading
            if previous is None or reading < previous:
                continue  # the first reading, or a meter reset
            deltas[role] = reading - previous
        offset = self._offset_w()
        corrected: dict[str, float] = {}
        if offset is not None and deltas:
            # Over the time since the previous boundary (longer after a restart).
            since = datetime.fromisoformat(last_tick) if same_day and last_tick else start
            corrected = offset_corrected(deltas, offset * (end - since) / timedelta(hours=1000))
        for role, delta in deltas.items():
            record = self._data["sides"][role]
            price = self._price_for(role, end)
            estimate = corrected.get(role, delta)
            record["kwh"] += delta
            record["estimate_kwh"] += estimate
            record["intervals"] += 1
            if role in corrected:
                record["corrected_intervals"] += 1
            if price is None:
                record["unpriced_kwh"] += delta
            else:
                record["amount"] += delta * price
                record["estimate_amount"] += estimate * price
        self._data["last_tick"] = end.isoformat()
        self._store.async_delay_save(lambda: self._data, SAVE_DELAY)
        self._on_update()

    def _roll(self, day: str, *, contiguous: bool) -> None:
        """Start ``day``: the previous day's estimate goes into the history. Readings
        carry over only when the previous interval was the day's last."""
        if self._data["day"] is not None:
            self.history[self._data["day"]] = {
                role: {k: v for k, v in record.items() if k not in ("reading", "entity_id")}
                for role, record in self._data["sides"].items()
            }
            for old in sorted(self.history)[:-HISTORY_DAYS]:
                del self.history[old]
        self._data["day"] = day
        for role, side in self.sides.items():
            reading = self._data["sides"][role]["reading"] if contiguous else None
            self._data["sides"][role] = {**_empty_side(side.energy_entity), "reading": reading}

    def summary(self, now: datetime) -> dict[str, Any]:
        """Today's estimate so far, per side."""
        today = nem_date(now)
        current = self._data["day"] == today.isoformat()
        elapsed = int((now - nem_day_start(today)) / INTERVAL)
        result: dict[str, Any] = {
            "date": today.isoformat(),
            "expected_intervals": min(elapsed, INTERVALS_PER_DAY),
            "last_interval_end": self._data["last_tick"],
            "meter": self.meter_name,
            "offset_w": self._offset_w(),
        }
        for role, side in self.sides.items():
            record = self._data["sides"][role] if current else _empty_side()
            result[role] = {
                "amount": round(record["estimate_amount"], 4),
                "kwh": round(record["estimate_kwh"], 3),
                "raw_estimate": round(record["amount"], 4),
                "raw_energy_kwh": round(record["kwh"], 3),
                "unpriced_kwh": round(record["unpriced_kwh"], 3),
                "intervals": record["intervals"],
                "offset_corrected_intervals": record["corrected_intervals"],
                "energy_entity": side.energy_entity,
                "price_entity": side.price_entity,
                "channel": side.channel,
            }
        return result


def _versus(estimated: float, billed: float, suffix: str) -> dict[str, Any]:
    """An estimate against the billed amount: the error (estimate minus billed)."""
    error = estimated - billed
    return {
        f"estimated_{suffix}": round(estimated, 4),
        f"error_{suffix}": round(error, 4),
        f"error_{suffix}_percent": round(100 * error / billed, 1) if billed else None,
    }


def compare(
    history: Mapping[str, Mapping[str, Any]],
    *,
    billed: Callable[[date], Mapping[str, Any] | None],
    role: str,
    amount_sid: str | None,
    energy_sid: str | None,
    before: date,
) -> dict[str, Any] | None:
    """The latest estimated day before ``before`` whose billed data has arrived, against
    the bill: the amount (cost, or feed-in credit) and the energy, with the error
    (estimate minus billed), for the offset-corrected estimate (the sensor's value) and
    the raw one."""
    for day in sorted(history, reverse=True):
        saved = history[day].get(role)
        if saved is None or date.fromisoformat(day) >= before:
            continue
        estimate = upgraded(saved)
        record = billed(date.fromisoformat(day)) or {}
        totals = record.get("totals") or {}
        if record.get("status") != "imported" or amount_sid not in totals:
            continue
        amount, kwh = totals[amount_sid], totals.get(energy_sid)
        return {
            "date": day,
            "billed": round(amount, 4),
            "billed_kwh": None if kwh is None else round(kwh, 3),
            **_versus(estimate["estimate_amount"], amount, "corrected"),
            "estimated_corrected_kwh": round(estimate["estimate_kwh"], 3),
            **_versus(estimate["amount"], amount, "raw"),
            "estimated_raw_kwh": round(estimate["kwh"], 3),
            "unpriced_kwh": round(estimate["unpriced_kwh"], 3),
            "intervals": estimate["intervals"],
            "offset_corrected_intervals": estimate["corrected_intervals"],
            "complete": estimate["intervals"] >= INTERVALS_PER_DAY
            and math.isclose(estimate["unpriced_kwh"], 0.0),
        }
    return None
