"""
backend/app/rules/deadlines.py

Детерминированный расчёт сроков по главе 6 Инструкции ДОУ (п. 6.3–6.7).
LLM здесь не участвует: она только извлекает параметры из фразы пользователя.

Допущение (проверьте с аналитиком): по умолчанию срок отсчитывается со дня,
СЛЕДУЮЩЕГО за датой регистрации (в п. 6.4 написано «начинается с даты регистрации»,
это можно прочесть и как «включая день регистрации»; для этого есть
флаг count_registration_day).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from functools import lru_cache
from pathlib import Path

CALENDAR_PATH = Path(__file__).with_name("calendar_ru.json")

_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
           "августа", "сентября", "октября", "ноября", "декабря"]
_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def fmt_date(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}, {_WEEKDAYS[d.weekday()]}"


# --------------------------------------------------------------------------- #
# Производственный календарь (офлайн)
# --------------------------------------------------------------------------- #
_FIXED_HOLIDAYS = [(1, d) for d in range(1, 9)] + [(2, 23), (3, 8), (5, 1), (5, 9), (6, 12), (11, 4)]


class WorkCalendar:
    """
    Год из calendar_ru.json (непустой "holidays") считается ОФИЦИАЛЬНЫМ:
      нерабочий день = в holidays, либо выходной не из "workdays" (рабочие сб/вс).
    Для остальных годов — расчёт по общим правилам (сб/вс + фиксированные праздники,
    праздник на выходной переносится на следующий рабочий день, кроме января).
    Такой календарь приблизительный: calendar_source == "computed".
    """

    def __init__(self, data: dict | None = None, path: Path | None = None) -> None:
        if data is None:
            p = path or CALENDAR_PATH
            data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        self._official: dict[int, tuple[set[date], set[date]]] = {}
        for key, val in data.items():
            if not str(key).isdigit() or not isinstance(val, dict):
                continue
            holidays = {date.fromisoformat(x) for x in val.get("holidays", [])}
            if holidays:  # пустой список = год не заполнен
                workdays = {date.fromisoformat(x) for x in val.get("workdays", [])}
                self._official[int(key)] = (holidays, workdays)
        self._computed_cache: dict[int, set[date]] = {}

    def _computed(self, year: int) -> set[date]:
        if year not in self._computed_cache:
            base = {date(year, m, d) for m, d in _FIXED_HOLIDAYS}
            result = set(base)
            for h in sorted(base):
                if h.month != 1 and h.weekday() >= 5:
                    nxt = h + timedelta(days=1)
                    while nxt.weekday() >= 5 or nxt in base or nxt in result:
                        nxt += timedelta(days=1)
                    result.add(nxt)
            self._computed_cache[year] = result
        return self._computed_cache[year]

    def source(self, d: date) -> str:
        return "official" if d.year in self._official else "computed"

    def is_working_day(self, d: date) -> bool:
        official = self._official.get(d.year)
        if official:
            holidays, workdays = official
            if d in holidays:
                return False
            return d.weekday() < 5 or d in workdays
        return d.weekday() < 5 and d not in self._computed(d.year)

    def prev_working_day(self, d: date) -> date:
        """d, если он рабочий, иначе ближайший предшествующий рабочий день."""
        while not self.is_working_day(d):
            d -= timedelta(days=1)
        return d

    def next_working_day(self, d: date) -> date:
        """Ближайший рабочий день СТРОГО после d."""
        d += timedelta(days=1)
        while not self.is_working_day(d):
            d += timedelta(days=1)
        return d

    def add_working_days(self, d: date, n: int) -> date:
        for _ in range(n):
            d = self.next_working_day(d)
        return d


@lru_cache(maxsize=1)
def get_calendar() -> WorkCalendar:
    return WorkCalendar()


# --------------------------------------------------------------------------- #
# Справочники (п. 6.6, 6.7)
# --------------------------------------------------------------------------- #
class Wording(str, Enum):
    BEFORE = "до"
    NOT_LATER = "не позднее"


class Urgency(str, Enum):
    URGENT = "срочно"
    VERY_URGENT = "весьма срочно"


class Kind(str, Enum):
    ADMIN_HEAD = "admin_head"
    FEDERAL_BODIES = "federal_bodies"
    GOVERNING_BODIES = "governing_bodies"
    REGULATORY_ACTS = "regulatory_acts"
    EXTERNAL_ORGS = "external_orgs"
    OTHER_LEGAL_ENTITIES = "other_legal_entities"
    CITIZEN_APPEALS = "citizen_appeals"
    INTERNAL_CORRESPONDENCE = "internal_correspondence"


@dataclass(frozen=True)
class Term:
    title: str
    days: int
    unit: str  # "calendar" | "working"


DEFAULT_TERMS: dict[Kind, Term] = {
    Kind.ADMIN_HEAD: Term("документы администрации высшего должностного лица государства", 15, "calendar"),
    Kind.FEDERAL_BODIES: Term("документы федеральных органов исполнительной власти и иных государственных органов", 25, "calendar"),
    Kind.GOVERNING_BODIES: Term("решения совета директоров и иных органов Организации", 25, "calendar"),
    Kind.REGULATORY_ACTS: Term("распорядительные акты", 30, "calendar"),
    Kind.EXTERNAL_ORGS: Term("документы внешних организаций и субъектов финансового рынка", 30, "calendar"),
    Kind.OTHER_LEGAL_ENTITIES: Term("документы иных юридических лиц", 30, "calendar"),
    Kind.CITIZEN_APPEALS: Term("обращения граждан", 30, "calendar"),
    Kind.INTERNAL_CORRESPONDENCE: Term("внутренняя служебная переписка", 10, "working"),
}


# --------------------------------------------------------------------------- #
# Результат
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DeadlineResult:
    deadline: date
    basis: str                  # пояснение со ссылками на пункты
    points: tuple[str, ...]     # использованные пункты Инструкции
    shifted: bool               # был ли перенос на предшествующий рабочий день
    calendar_source: str        # "official" | "computed"
    notes: tuple[str, ...] = ()

    @property
    def deadline_text(self) -> str:
        return fmt_date(self.deadline)


def _shift_text(last: date, final: date) -> str:
    return (f"{fmt_date(last)} — выходной или нерабочий праздничный день, поэтому срок "
            f"завершается в предшествующий рабочий день: {fmt_date(final)} (п. 6.4).")


# --------------------------------------------------------------------------- #
# Публичные функции
# --------------------------------------------------------------------------- #
def deadline_from_date(target: date, wording: Wording | str, cal: WorkCalendar | None = None) -> DeadlineResult:
    """«до 14 октября» -> 13 октября; «не позднее 14 октября» -> 14 октября (п. 6.4)."""
    cal = cal or get_calendar()
    wording = Wording(wording)  # ValueError на незнакомую формулировку
    if wording is Wording.BEFORE:
        last = target - timedelta(days=1)
        parts = [f"Формулировка «до {fmt_date(target)}» не включает указанную дату, "
                 f"поэтому последний день — {fmt_date(last)} (п. 6.4)."]
    else:
        last = target
        parts = [f"Формулировка «не позднее {fmt_date(target)}» включает указанную дату — "
                 f"последний день {fmt_date(last)} (п. 6.4)."]
    final = cal.prev_working_day(last)
    shifted = final != last
    if shifted:
        parts.append(_shift_text(last, final))
    return DeadlineResult(final, " ".join(parts), ("6.4",), shifted, cal.source(final))


def deadline_default(
    kind: Kind | str,
    registered: date,
    urgency: Urgency | str | None = None,
    cal: WorkCalendar | None = None,
    count_registration_day: bool = False,
) -> DeadlineResult:
    """Предельный срок исполнения от даты регистрации, когда специальный срок не указан (п. 6.6–6.7)."""
    cal = cal or get_calendar()

    if urgency is not None:
        urgency = Urgency(urgency)
        note = "Отметки срочности (п. 6.7) относятся к документам, поступающим от органов государственной власти."
        if urgency is Urgency.VERY_URGENT:
            final = cal.next_working_day(registered)
            basis = (f"Отметка «Весьма срочно»: не позднее рабочего дня, следующего за датой "
                     f"регистрации ({fmt_date(registered)}) — {fmt_date(final)} (п. 6.7).")
        else:
            final = cal.add_working_days(registered, 3)
            basis = (f"Отметка «Срочно»: 3 рабочих дня с даты регистрации "
                     f"({fmt_date(registered)}) — {fmt_date(final)} (п. 6.7).")
        return DeadlineResult(final, basis, ("6.7",), False, cal.source(final), (note,))

    term = DEFAULT_TERMS[Kind(kind)]
    offset = term.days - 1 if count_registration_day else term.days
    count_note = ("Срок отсчитывается со дня, следующего за датой регистрации."
                  if not count_registration_day else "День регистрации включён в срок.")

    if term.unit == "working":
        final = cal.add_working_days(registered, offset)
        basis = (f"{term.title.capitalize()}: {term.days} рабочих дней с даты регистрации "
                 f"({fmt_date(registered)}) — {fmt_date(final)} (п. 6.6).")
        return DeadlineResult(final, basis, ("6.6",), False, cal.source(final), (count_note,))

    nominal = registered + timedelta(days=offset)
    final = cal.prev_working_day(nominal)
    shifted = final != nominal
    basis = (f"{term.title.capitalize()}: {term.days} календарных дней с даты регистрации "
             f"({fmt_date(registered)}) — {fmt_date(nominal)} (п. 6.6).")
    points = ["6.6"]
    if shifted:
        basis += " " + _shift_text(nominal, final)
        points.append("6.4")
    return DeadlineResult(final, basis, tuple(points), shifted, cal.source(final), (count_note,))


def coexecutor_deadline(registered: date, final_deadline: date, cal: WorkCalendar | None = None) -> DeadlineResult:
    """Внутренний срок соисполнителя: как правило, в пределах половины общего срока (п. 6.7)."""
    cal = cal or get_calendar()
    half = registered + timedelta(days=(final_deadline - registered).days // 2)
    final = cal.prev_working_day(half)
    basis = (f"Организация — соисполнитель: внутренний срок, как правило, в пределах половины общего срока "
             f"({fmt_date(registered)} — {fmt_date(final_deadline)}), то есть до {fmt_date(final)} (п. 6.7).")
    return DeadlineResult(final, basis, ("6.7",), final != half, cal.source(final),
                          ("Формулировка «как правило» в п. 6.7 допускает исключения.",))