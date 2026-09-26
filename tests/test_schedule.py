"""Schedule tests: stable seeded offsets, fixed times and daylight saving."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.amber_energy_dashboard.const import (
    AUTO_WINDOW_LENGTH_MINUTES,
    AUTO_WINDOW_START_MINUTES,
)
from custom_components.amber_energy_dashboard.schedule import (
    ScheduleConfig,
    attempts_on,
    format_times,
    is_final_attempt,
    local_attempt_times,
    next_attempt,
    parse_times,
)
from custom_components.amber_energy_dashboard.storage import schedule_seed_for

SYDNEY = ZoneInfo("Australia/Sydney")
ADELAIDE = ZoneInfo("Australia/Adelaide")
AUTO = ScheduleConfig()
FIXED = ScheduleConfig("fixed", (time(7, 15), time(10, 15), time(13, 15)))


def test_seed_is_stable_and_spreads() -> None:
    """The seed depends only on the entry ID, and different installs spread out."""
    assert schedule_seed_for("01ABC") == schedule_seed_for("01ABC")
    firsts = {local_attempt_times(AUTO, schedule_seed_for(f"entry{i}"))[0] for i in range(200)}
    assert len(firsts) > 60  # spread across the 120-minute window


@pytest.mark.parametrize("entry_id", ["01ABC", "01M3ENRSQFCSBZ4G4KWNE9NEJX", "x"])
def test_automatic_times_in_window_with_retries(entry_id: str) -> None:
    """First attempt inside 06:30 to 08:30; retries exactly 3 h and 6 h later."""
    first, retry1, retry2 = local_attempt_times(AUTO, schedule_seed_for(entry_id))
    minutes = first.hour * 60 + first.minute
    assert (
        AUTO_WINDOW_START_MINUTES
        <= minutes
        < AUTO_WINDOW_START_MINUTES + AUTO_WINDOW_LENGTH_MINUTES
    )
    assert retry1.hour * 60 + retry1.minute == minutes + 180
    assert retry2.hour * 60 + retry2.minute == minutes + 360


def test_same_entry_same_times_every_day() -> None:
    """The offset does not drift between days."""
    seed = schedule_seed_for("01ABC")
    local = {
        tuple(
            a.astimezone(SYDNEY).time() for a in attempts_on(date(2026, 9, d), AUTO, seed, SYDNEY)
        )
        for d in range(1, 30)
    }
    assert len(local) == 1


def test_fixed_times_and_next_attempt() -> None:
    """Fixed times are used as given; next_attempt walks through the day, then tomorrow."""
    assert local_attempt_times(FIXED, 123) == FIXED.fixed_times
    now = datetime(2026, 9, 26, 8, 0, tzinfo=SYDNEY).astimezone(UTC)
    assert next_attempt(now, FIXED, 0, SYDNEY) == datetime(2026, 9, 26, 10, 15, tzinfo=SYDNEY)
    after_last = datetime(2026, 9, 26, 13, 15, tzinfo=SYDNEY).astimezone(UTC)
    assert next_attempt(after_last, FIXED, 0, SYDNEY) == datetime(2026, 9, 27, 7, 15, tzinfo=SYDNEY)
    assert is_final_attempt(after_last, FIXED, 0, SYDNEY)
    assert not is_final_attempt(now, FIXED, 0, SYDNEY)


@pytest.mark.parametrize("tz", [SYDNEY, ADELAIDE], ids=["Sydney", "Adelaide"])
def test_times_hold_local_across_october_2026_dst(tz: ZoneInfo) -> None:
    """On 4 Oct 2026 clocks go forward: local times stay put, UTC instants move 1 h."""
    before = attempts_on(date(2026, 10, 3), FIXED, 0, tz)
    after = attempts_on(date(2026, 10, 4), FIXED, 0, tz)
    assert [a.astimezone(tz).time() for a in before] == list(FIXED.fixed_times)
    assert [a.astimezone(tz).time() for a in after] == list(FIXED.fixed_times)
    for b, a in zip(before, after, strict=True):
        assert a - b == timedelta(hours=23)  # the local day is 23 hours long
    # Walking next_attempt across the change never skips or repeats an attempt.
    now = datetime(2026, 10, 3, 14, 0, tzinfo=tz).astimezone(UTC)
    seen = []
    for _ in range(6):
        now = next_attempt(now, FIXED, 0, tz)
        seen.append(now.astimezone(tz).strftime("%m-%d %H:%M"))
    assert seen == [
        "10-04 07:15",
        "10-04 10:15",
        "10-04 13:15",
        "10-05 07:15",
        "10-05 10:15",
        "10-05 13:15",
    ]


def test_sydney_utc_instants_around_dst() -> None:
    """Concrete UTC values: 07:15 AEST is 21:15Z; 07:15 AEDT is 20:15Z."""
    assert attempts_on(date(2026, 10, 3), FIXED, 0, SYDNEY)[0] == datetime(
        2026, 10, 2, 21, 15, tzinfo=UTC
    )
    assert attempts_on(date(2026, 10, 4), FIXED, 0, SYDNEY)[0] == datetime(
        2026, 10, 3, 20, 15, tzinfo=UTC
    )


def test_time_in_spring_forward_gap() -> None:
    """02:30 does not exist on 4 Oct 2026 in Sydney; it maps to 03:30 AEDT (fold=0)."""
    gap = ScheduleConfig("fixed", (time(2, 30),))
    [when] = attempts_on(date(2026, 10, 4), gap, 0, SYDNEY)
    assert when == datetime(2026, 10, 3, 16, 30, tzinfo=UTC)
    assert when.astimezone(SYDNEY).strftime("%H:%M %Z") == "03:30 AEDT"


def test_automatic_across_dst_keeps_local_time() -> None:
    """The automatic first attempt keeps its local time across the change."""
    seed = schedule_seed_for("01ABC")
    first_before = attempts_on(date(2026, 10, 3), AUTO, seed, SYDNEY)[0]
    first_after = attempts_on(date(2026, 10, 4), AUTO, seed, SYDNEY)[0]
    assert first_before.astimezone(SYDNEY).time() == first_after.astimezone(SYDNEY).time()
    assert first_after - first_before == timedelta(hours=23)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("07:15, 10:15, 13:15", "07:15, 10:15, 13:15"),
        ("13:15,7:15;10:15,07:15", "07:15, 10:15, 13:15"),
        (["23:59", "00:00"], "00:00, 23:59"),
    ],
)
def test_parse_times(text, expected: str) -> None:
    """Times are normalised, de-duplicated and sorted."""
    assert format_times(parse_times(text)) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " , ",
        "24:00",
        "7.15",
        "07:60",
        "1,2",
        ",".join(["01:00"] * 1 + [f"0{i}:00" for i in range(2, 9)]),
    ],
)
def test_parse_times_rejects(bad: str) -> None:
    """Invalid, empty or too many times are rejected."""
    with pytest.raises(ValueError):
        parse_times(bad)
