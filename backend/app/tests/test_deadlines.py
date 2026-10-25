from datetime import date

import pytest

from app.rules.deadlines import (Kind, Urgency, WorkCalendar, Wording,
                                 coexecutor_deadline, deadline_default, deadline_from_date)

CAL = WorkCalendar(data={})  # расчётный календарь
OFFICIAL = WorkCalendar(data={"2026": {"holidays": ["2026-10-13"], "workdays": ["2026-10-10"]}})
REG = date(2026, 10, 14)  # среда


def test_before_excludes_date():
    r = deadline_from_date(date(2026, 10, 14), "до", CAL)
    assert r.deadline == date(2026, 10, 13) and not r.shifted


def test_not_later_includes_date():
    r = deadline_from_date(date(2026, 10, 14), Wording.NOT_LATER, CAL)
    assert r.deadline == date(2026, 10, 14)


def test_before_lands_on_weekend_moves_to_friday():
    r = deadline_from_date(date(2026, 10, 12), "до", CAL)  # 11.10 — воскресенье
    assert r.deadline == date(2026, 10, 9) and r.shifted


def test_not_later_on_sunday_moves_to_friday():
    assert deadline_from_date(date(2026, 10, 11), "не позднее", CAL).deadline == date(2026, 10, 9)


def test_holiday_may_first():
    assert deadline_from_date(date(2026, 5, 1), "не позднее", CAL).deadline == date(2026, 4, 30)
    assert deadline_from_date(date(2026, 5, 2), "до", CAL).deadline == date(2026, 4, 30)


def test_january_holidays_fallback():
    assert deadline_from_date(date(2027, 1, 5), "не позднее", CAL).deadline == date(2026, 12, 31)


def test_computed_holiday_carryover():
    assert not CAL.is_working_day(date(2026, 5, 11))  # 9 мая — суббота
    assert not CAL.is_working_day(date(2026, 3, 9))   # 8 марта — воскресенье
    assert CAL.is_working_day(date(2026, 5, 8))


def test_official_calendar_used():
    r = deadline_from_date(date(2026, 10, 14), "до", OFFICIAL)  # 13.10 — праздник
    assert r.deadline == date(2026, 10, 12) and r.shifted and r.calendar_source == "official"
    assert OFFICIAL.is_working_day(date(2026, 10, 10))  # рабочая суббота


def test_calendar_source_computed():
    assert deadline_from_date(date(2026, 10, 14), "до", CAL).calendar_source == "computed"


def test_calendar_days_terms():
    assert deadline_default(Kind.ADMIN_HEAD, REG, cal=CAL).deadline == date(2026, 10, 29)
    assert deadline_default(Kind.REGULATORY_ACTS, REG, cal=CAL).deadline == date(2026, 11, 13)


def test_calendar_days_shift_from_weekend():
    r = deadline_default("federal_bodies", REG, cal=CAL)  # 25 дн -> 08.11 (вс)
    assert r.deadline == date(2026, 11, 6) and r.shifted and "6.4" in r.points


def test_internal_correspondence_working_days():
    assert deadline_default(Kind.INTERNAL_CORRESPONDENCE, REG, cal=CAL).deadline == date(2026, 10, 28)


def test_count_registration_day_flag():
    r = deadline_default(Kind.INTERNAL_CORRESPONDENCE, REG, cal=CAL, count_registration_day=True)
    assert r.deadline == date(2026, 10, 27)


def test_urgency():
    friday = date(2026, 10, 9)
    assert deadline_default(Kind.FEDERAL_BODIES, friday, Urgency.URGENT, CAL).deadline == date(2026, 10, 14)
    assert deadline_default(Kind.FEDERAL_BODIES, friday, "весьма срочно", CAL).deadline == date(2026, 10, 12)


def test_coexecutor_half_term():
    assert coexecutor_deadline(REG, date(2026, 11, 13), CAL).deadline == date(2026, 10, 29)


def test_unknown_wording_raises():
    with pytest.raises(ValueError):
        deadline_from_date(date(2026, 10, 14), "после", CAL)