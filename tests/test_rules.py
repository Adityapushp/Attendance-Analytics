"""Pure unit tests for the business rules R1-R5 and the rounding helpers (no database needed)."""
from datetime import datetime, timezone
from decimal import Decimal

from app.main import (IST, attendance_date, compute_late_minutes, compute_overtime_minutes,
                      compute_work_hours, count_weekdays, derive, from_ms, round_half_up, to_ms)
from datetime import date

DAY = {"shift_start": "09:30", "shift_end": "18:30"}
NIGHT = {"shift_start": "22:00", "shift_end": "06:00"}


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


def test_late_grace_boundary_r2():
    d = "2026-07-06"
    assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 0), d, DAY) == 0
    assert compute_late_minutes(ist(2026, 7, 6, 9, 40, 1), d, DAY) == 10
    assert compute_late_minutes(ist(2026, 7, 6, 10, 15, 59), d, DAY) == 45
    assert compute_late_minutes(ist(2026, 7, 6, 8, 0, 0), d, DAY) == 0


def test_overtime_threshold_r3():
    d = "2026-07-06"
    assert compute_overtime_minutes(ist(2026, 7, 6, 18, 59, 59), d, DAY) == 0   # 29 min
    assert compute_overtime_minutes(ist(2026, 7, 6, 19, 0, 0), d, DAY) == 30
    assert compute_overtime_minutes(ist(2026, 7, 6, 19, 10, 59), d, DAY) == 40


def test_work_hours_half_up_r4():
    # 9h 00m 18s = 9.005 h -> half-up 9.01 (float round() gives 9.0)
    assert compute_work_hours(ist(2026, 7, 6, 9, 30), ist(2026, 7, 6, 18, 30, 18)) == Decimal("9.01")
    assert round_half_up(2.675, 2) == 2.68 and round_half_up(0.125, 2) == 0.13


def test_half_day_uses_rounded_hours_r5():
    emp = DAY
    out = derive(emp, "2026-07-06", "PRESENT", ist(2026, 7, 6, 9, 30), ist(2026, 7, 6, 13, 59, 59))
    assert out["work_hours"] == 4.5 and out["half_day"] is False      # 4.4997 rounds to 4.50
    out = derive(emp, "2026-07-06", "PRESENT", ist(2026, 7, 6, 9, 30), ist(2026, 7, 6, 13, 59, 0))
    assert out["half_day"] is True                                    # 4.4833 -> 4.48


def test_overnight_shift_r1():
    # punch-in 05:00 IST on the 7th belongs to the shift that started on the 6th
    assert attendance_date(ist(2026, 7, 7, 5, 0), NIGHT) == "2026-07-06"
    assert attendance_date(ist(2026, 7, 7, 6, 0), NIGHT) == "2026-07-07"
    assert attendance_date(ist(2026, 7, 6, 21, 55), NIGHT) == "2026-07-06"
    # the sample document 7 from DATA_MODEL.md
    out = derive(NIGHT, "2026-07-06", "PRESENT", ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40))
    assert out == {"work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40, "half_day": False}


def test_date_is_ist_not_utc():
    # 20:00 UTC on the 6th is 01:30 IST on the 7th
    ts = datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc)
    assert attendance_date(ts, DAY) == "2026-07-07"


def test_ms_conversion_truncates_to_seconds():
    assert to_ms(from_ms(1783312500999)) == 1783312500000


def test_sample_documents_from_data_model():
    # sample 2: late with overtime;  sample 4: half day
    out = derive(DAY, "2026-07-06", "PRESENT", ist(2026, 7, 6, 10, 5), ist(2026, 7, 6, 19, 10))
    assert (out["late_minutes"], out["overtime_minutes"], out["work_hours"]) == (35, 40, 9.08)
    out = derive(DAY, "2026-07-07", "WFH", ist(2026, 7, 7, 9, 30), ist(2026, 7, 7, 13, 0))
    assert out["half_day"] is True and out["work_hours"] == 3.5


def test_weekday_count_r7():
    assert count_weekdays(date(2026, 7, 1), date(2026, 7, 31)) == 23
    assert count_weekdays(date(2026, 7, 13), date(2026, 7, 31)) == 15   # mid-month joiner
    assert count_weekdays(date(2026, 8, 5), date(2026, 7, 31)) == 0
