"""Расписания покупок: разбор человекочитаемой записи и расчёт следующего запуска.

Поддерживаемые форматы (регистр не важен, можно по-русски или по-английски):

    ежедневно 12:00                daily 12:00
    ежедневно 09:00, 21:00         daily 09:00,21:00
    пн, ср, пт 18:30               mon,wed,fri 18:30
    пн-пт 10:00                    mon-fri 10:00
    будни 10:00 / выходные 11:00   weekdays 10:00 / weekends 11:00
    ежемесячно 1, 15 10:00         monthly 1,15 10:00
    разово 10.11.2026 12:00        once 2026-11-10 12:00

Если в месяце нет указанного числа (например, 31-го), покупка будет в последний день месяца.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

Kind = Literal["daily", "weekly", "monthly", "once"]

WEEKDAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_SHORT_RU = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

_WEEKDAY_ALIASES: dict[str, int] = {}
for _index, _names in enumerate(
    (
        ("пн", "пон", "понедельник", "понедельникам", "mon", "monday"),
        ("вт", "вторник", "вторникам", "tue", "tuesday"),
        ("ср", "среда", "среду", "средам", "wed", "wednesday"),
        ("чт", "четверг", "четвергам", "thu", "thursday"),
        ("пт", "пятница", "пятницу", "пятницам", "fri", "friday"),
        ("сб", "суббота", "субботу", "субботам", "sat", "saturday"),
        ("вс", "воскресенье", "воскресеньям", "sun", "sunday"),
    )
):
    for _name in _names:
        _WEEKDAY_ALIASES[_name] = _index

_WEEKDAY_GROUPS = {
    "будни": (0, 1, 2, 3, 4),
    "будням": (0, 1, 2, 3, 4),
    "weekdays": (0, 1, 2, 3, 4),
    "выходные": (5, 6),
    "выходным": (5, 6),
    "weekends": (5, 6),
}
_DAILY_WORDS = {"ежедневно", "daily"}
_WEEKLY_WORDS = {"еженедельно", "weekly"}
_MONTHLY_WORDS = {"ежемесячно", "monthly"}
_ONCE_WORDS = {"разово", "однократно", "once"}
_FILLER_WORDS = {"в", "во", "at", "по", "on", "числа", "число", "числам"}

_TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_RU_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")

_ALL_WEEK = (0, 1, 2, 3, 4, 5, 6)
_SEARCH_DAYS = 400


class ScheduleError(ValueError):
    """Неверная запись расписания. Текст ошибки показывается пользователю."""


@dataclass(frozen=True)
class Schedule:
    kind: Kind
    times: tuple[time, ...]
    weekdays: tuple[int, ...] = ()
    monthdays: tuple[int, ...] = ()
    on_date: date | None = None

    # ---- представление -------------------------------------------------

    def to_spec(self) -> str:
        """Каноническая запись: её же понимает parse_schedule()."""
        times = ",".join(t.strftime("%H:%M") for t in self.times)
        if self.kind == "daily":
            return f"daily {times}"
        if self.kind == "weekly":
            return f"weekly {','.join(WEEKDAY_CODES[d] for d in self.weekdays)} {times}"
        if self.kind == "monthly":
            return f"monthly {','.join(str(d) for d in self.monthdays)} {times}"
        assert self.on_date is not None
        return f"once {self.on_date.isoformat()} {times}"

    def describe(self) -> str:
        times = ", ".join(t.strftime("%H:%M") for t in self.times)
        if self.kind == "daily":
            return f"ежедневно в {times}"
        if self.kind == "weekly":
            if self.weekdays == (0, 1, 2, 3, 4):
                days = "по будням"
            elif self.weekdays == (5, 6):
                days = "по выходным"
            else:
                days = "по " + ", ".join(WEEKDAY_SHORT_RU[d] for d in self.weekdays)
            return f"{days} в {times}"
        if self.kind == "monthly":
            return f"ежемесячно {', '.join(str(d) for d in self.monthdays)} числа в {times}"
        assert self.on_date is not None
        return f"разово {self.on_date:%d.%m.%Y} в {times}"

    # ---- расчёт запусков ---------------------------------------------

    def next_after(self, after: datetime, tz: ZoneInfo) -> datetime | None:
        """Ближайший запуск строго позже `after` (в UTC) или None, если запусков больше нет."""
        if after.tzinfo is None:
            raise ValueError("after должен быть с часовым поясом")
        if self.kind == "once":
            assert self.on_date is not None
            candidate = _at(self.on_date, self.times[0], tz)
            return candidate if candidate > after else None

        start = after.astimezone(tz).date() - timedelta(days=1)
        found: list[datetime] = []
        first_hit: int | None = None
        for offset in range(_SEARCH_DAYS):
            day = start + timedelta(days=offset)
            if self._matches(day):
                found.extend(c for c in (_at(day, t, tz) for t in self.times) if c > after)
            if found and first_hit is None:
                first_hit = offset
            # Переход на летнее время может «переставить» соседние сутки — смотрим ещё один день.
            if first_hit is not None and offset > first_hit:
                break
        return min(found) if found else None

    def upcoming(self, after: datetime, tz: ZoneInfo, count: int) -> list[datetime]:
        result: list[datetime] = []
        moment = after
        while len(result) < count:
            nxt = self.next_after(moment, tz)
            if nxt is None:
                break
            result.append(nxt)
            moment = nxt
        return result

    def _matches(self, day: date) -> bool:
        if self.kind == "daily":
            return True
        if self.kind == "weekly":
            return day.weekday() in self.weekdays
        last = calendar.monthrange(day.year, day.month)[1]
        return day.day in {min(d, last) for d in self.monthdays}


def _at(day: date, moment: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, moment, tzinfo=tz).astimezone(UTC)


# ---- разбор ---------------------------------------------------------------


def parse_schedule(text: str) -> Schedule:
    tokens = _tokenize(text)
    if not tokens:
        raise ScheduleError("Расписание пустое.")
    head, rest = tokens[0], tokens[1:]

    if head in _DAILY_WORDS:
        return Schedule("daily", _parse_times(rest))

    if head in _MONTHLY_WORDS:
        days: list[int] = []
        while rest and rest[0].isdigit():
            day = int(rest.pop(0))
            if not 1 <= day <= 31:
                raise ScheduleError(f"Число месяца должно быть от 1 до 31, а не {day}.")
            days.append(day)
        if not days:
            raise ScheduleError("Укажите числа месяца, например: ежемесячно 1, 15 10:00")
        return Schedule("monthly", _parse_times(rest), monthdays=tuple(sorted(set(days))))

    if head in _ONCE_WORDS:
        if len(rest) != 2:
            raise ScheduleError("Для разовой покупки нужны дата и время: разово 10.11.2026 12:00")
        on_date = _parse_date(rest[0])
        return Schedule("once", _parse_times(rest[1:]), on_date=on_date)

    if head in _WEEKLY_WORDS:
        tokens = rest
    weekdays: set[int] = set()
    while tokens and not _TIME_RE.match(tokens[0]):
        weekdays.update(_parse_weekday_token(tokens.pop(0)))
    if not weekdays:
        raise ScheduleError("Укажите дни недели, например: пн, чт 18:30")
    times = _parse_times(tokens)
    ordered = tuple(sorted(weekdays))
    if ordered == _ALL_WEEK:
        return Schedule("daily", times)
    return Schedule("weekly", times, weekdays=ordered)


def _tokenize(text: str) -> list[str]:
    normalized = text.strip().lower().replace("ё", "е")
    normalized = re.sub(r"\bкаждый\s+день\b", "ежедневно", normalized)
    normalized = re.sub(r"\bevery\s+day\b", "daily", normalized)
    normalized = re.sub(r"\s*[-–—]\s*", "-", normalized)  # «пн - пт» → «пн-пт»
    tokens = [t for t in re.split(r"[\s,;]+", normalized) if t]
    return [t for t in tokens if t not in _FILLER_WORDS]


def _parse_times(tokens: list[str]) -> tuple[time, ...]:
    if not tokens:
        raise ScheduleError("Укажите время, например 12:00")
    result: set[time] = set()
    for token in tokens:
        match = _TIME_RE.match(token)
        if not match:
            raise ScheduleError(f"Не понял «{token}». Время пишется так: 12:00")
        hour, minute = int(match[1]), int(match[2])
        if hour > 23 or minute > 59:
            raise ScheduleError(f"Нет такого времени: {token}")
        result.add(time(hour, minute))
    return tuple(sorted(result))


def _parse_weekday_token(token: str) -> tuple[int, ...]:
    if token in _WEEKDAY_GROUPS:
        return _WEEKDAY_GROUPS[token]
    if token in _WEEKDAY_ALIASES:
        return (_WEEKDAY_ALIASES[token],)
    if "-" in token:
        first, _, last = token.partition("-")
        if first in _WEEKDAY_ALIASES and last in _WEEKDAY_ALIASES:
            start, end = _WEEKDAY_ALIASES[first], _WEEKDAY_ALIASES[last]
            if start <= end:
                return tuple(range(start, end + 1))
            return (*range(start, 7), *range(0, end + 1))
    raise ScheduleError(
        f"Не понял «{token}». Примеры: ежедневно 12:00 · пн, чт 18:30 · ежемесячно 1 10:00"
    )


def _parse_date(token: str) -> date:
    if match := _ISO_DATE_RE.match(token):
        year, month, day = int(match[1]), int(match[2]), int(match[3])
    elif match := _RU_DATE_RE.match(token):
        day, month, year = int(match[1]), int(match[2]), int(match[3])
    else:
        raise ScheduleError(f"Не понял дату «{token}». Пишите так: 10.11.2026")
    try:
        return date(year, month, day)
    except ValueError:
        raise ScheduleError(f"Нет такой даты: {token}") from None
