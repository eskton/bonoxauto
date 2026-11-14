from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from bonoxauto.schedule import ScheduleError, parse_schedule

MSK = ZoneInfo("Europe/Moscow")
BERLIN = ZoneInfo("Europe/Berlin")


@pytest.mark.parametrize(
    ("text", "spec"),
    [
        ("ежедневно 12:00", "daily 12:00"),
        ("Ежедневно в 9:05", "daily 09:05"),
        ("каждый день 21:00, 09:00", "daily 09:00,21:00"),
        ("daily 12.30", "daily 12:30"),
        ("пн, ср, пт 18:30", "weekly mon,wed,fri 18:30"),
        ("по понедельникам в 10:00", "weekly mon 10:00"),
        ("пн-пт 10:00", "weekly mon,tue,wed,thu,fri 10:00"),
        ("пн - ср 10:00", "weekly mon,tue,wed 10:00"),
        ("сб-пн 11:00", "weekly mon,sat,sun 11:00"),
        ("будни 08:00", "weekly mon,tue,wed,thu,fri 08:00"),
        ("по выходным в 11:00", "weekly sat,sun 11:00"),
        ("weekly tue,thu 07:15", "weekly tue,thu 07:15"),
        ("будни выходные 10:00", "daily 10:00"),
        ("ежемесячно 15, 1 10:00", "monthly 1,15 10:00"),
        ("ежемесячно 31 числа в 23:59", "monthly 31 23:59"),
        ("monthly 5 06:00,18:00", "monthly 5 06:00,18:00"),
        ("разово 10.11.2026 12:00", "once 2026-11-10 12:00"),
        ("once 2026-11-10 12:00", "once 2026-11-10 12:00"),
    ],
)
def test_parse_to_canonical_spec(text: str, spec: str) -> None:
    schedule = parse_schedule(text)
    assert schedule.to_spec() == spec
    assert parse_schedule(spec) == schedule  # каноническая запись читается обратно


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ежедневно",
        "ежедневно 25:00",
        "ежедневно 12:60",
        "ежедневно полдень",
        "пн",
        "12:00",
        "когда-нибудь 12:00",
        "ежемесячно 10:00",
        "ежемесячно 32 10:00",
        "разово 31.02.2026 12:00",
        "разово 2026-11-10",
        "разово завтра 12:00",
    ],
)
def test_parse_errors(text: str) -> None:
    with pytest.raises(ScheduleError):
        parse_schedule(text)


def test_describe() -> None:
    assert parse_schedule("daily 09:00,21:00").describe() == "ежедневно в 09:00, 21:00"
    assert parse_schedule("пн-пт 10:00").describe() == "по будням в 10:00"
    assert parse_schedule("сб, вс 10:00").describe() == "по выходным в 10:00"
    assert parse_schedule("пн, чт 18:30").describe() == "по пн, чт в 18:30"
    assert parse_schedule("ежемесячно 1, 15 10:00").describe() == "ежемесячно 1, 15 числа в 10:00"
    assert parse_schedule("разово 10.11.2026 12:00").describe() == "разово 10.11.2026 в 12:00"


def test_daily_next_run_today_and_tomorrow() -> None:
    schedule = parse_schedule("ежедневно 12:00")
    # 08:00 UTC = 11:00 МСК → сегодня в 12:00 МСК (09:00 UTC)
    assert schedule.next_after(datetime(2026, 11, 3, 8, 0, tzinfo=UTC), MSK) == datetime(
        2026, 11, 3, 9, 0, tzinfo=UTC
    )
    # ровно в момент запуска — следующий уже завтра
    assert schedule.next_after(datetime(2026, 11, 3, 9, 0, tzinfo=UTC), MSK) == datetime(
        2026, 11, 4, 9, 0, tzinfo=UTC
    )


def test_daily_multiple_times() -> None:
    schedule = parse_schedule("daily 09:00,21:00")
    runs = schedule.upcoming(datetime(2026, 11, 3, 10, 0, tzinfo=UTC), ZoneInfo("UTC"), 3)
    assert runs == [
        datetime(2026, 11, 3, 21, 0, tzinfo=UTC),
        datetime(2026, 11, 4, 9, 0, tzinfo=UTC),
        datetime(2026, 11, 4, 21, 0, tzinfo=UTC),
    ]


def test_weekly_next_run() -> None:
    schedule = parse_schedule("пн, чт 18:30")
    # 2026-11-03 — вторник
    runs = schedule.upcoming(datetime(2026, 11, 3, 0, 0, tzinfo=UTC), ZoneInfo("UTC"), 3)
    assert [r.date() for r in runs] == [date(2026, 11, 5), date(2026, 11, 9), date(2026, 11, 12)]
    assert all(r.time() == time(18, 30) for r in runs)


def test_monthly_clamps_to_last_day() -> None:
    schedule = parse_schedule("ежемесячно 31 10:00")
    runs = schedule.upcoming(datetime(2027, 1, 31, 12, 0, tzinfo=UTC), ZoneInfo("UTC"), 3)
    assert [r.date() for r in runs] == [date(2027, 2, 28), date(2027, 3, 31), date(2027, 4, 30)]


def test_monthly_31_and_30_do_not_double_fire() -> None:
    schedule = parse_schedule("ежемесячно 30, 31 10:00")
    runs = schedule.upcoming(datetime(2027, 2, 1, 0, 0, tzinfo=UTC), ZoneInfo("UTC"), 2)
    assert [r.date() for r in runs] == [date(2027, 2, 28), date(2027, 3, 30)]


def test_once() -> None:
    schedule = parse_schedule("разово 10.11.2026 12:00")
    first = schedule.next_after(datetime(2026, 11, 3, tzinfo=UTC), MSK)
    assert first == datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
    assert schedule.next_after(first, MSK) is None
    assert schedule.upcoming(datetime(2026, 11, 3, tzinfo=UTC), MSK, 5) == [first]


def test_dst_spring_forward_gap_and_ordering() -> None:
    # В Берлине 29.03.2026 часы переводят с 02:00 на 03:00 — 02:30 не существует.
    schedule = parse_schedule("ежедневно 02:30, 03:00")
    after = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
    runs = schedule.upcoming(after, BERLIN, 3)
    assert runs == sorted(runs)
    assert len(set(runs)) == 3
    assert all(r > after for r in runs)


def test_dst_fall_back_fires_once() -> None:
    # 25.10.2026 в Берлине 02:30 бывает дважды — запуск должен быть один.
    schedule = parse_schedule("ежедневно 02:30")
    after = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    first, second = schedule.upcoming(after, BERLIN, 2)
    assert first.astimezone(BERLIN).date() == date(2026, 10, 25)
    assert second.astimezone(BERLIN).date() == date(2026, 10, 26)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        parse_schedule("daily 12:00").next_after(datetime(2026, 1, 1), MSK)
