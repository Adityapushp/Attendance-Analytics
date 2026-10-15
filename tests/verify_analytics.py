"""
Run the analytics endpoints against a REAL MongoDB (6.0+) and compare them with a plain-Python
reference implementation written straight from the rules in openapi.yaml.

    export MONGO_URI="mongodb://localhost:27017"      # local docker: docker run -p 27017:27017 mongo:7
    python tests/verify_analytics.py                  # uses (and DROPS) the database 'attendance_verify'

It seeds awkward data: ties, weekend records, a mid-month joiner, a future joiner, an overnight shift,
an employee with no logs, a missing weekday, half days, and logs of an unknown employee. It also fires
concurrent requests and checks every explain plan for IXSCAN / no COLLSCAN.
"""
import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

os.environ["MONGO_DB"] = os.environ.get("MONGO_DB", "attendance_verify")
assert "verify" in os.environ["MONGO_DB"], "refusing to drop a database whose name lacks 'verify'"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402

PRES = ("PRESENT", "WFH", "ON_DUTY")
MONTH, FIRST, LAST = "2026-07", date(2026, 7, 1), date(2026, 7, 31)


def hu(x, p):
    return float(Decimal(x).quantize(Decimal(1).scaleb(-p), rounding=ROUND_HALF_UP))


def seed():
    m.client.drop_database(os.environ["MONGO_DB"])
    m.ensure_indexes()
    emps = [
        ("EMP0001", "Asha", "Eng", "09:30", "18:30", "2026-01-05"),
        ("EMP0002", "Vikram", "Eng", "09:30", "18:30", "2026-07-13"),   # mid-month joiner
        ("EMP0003", "Neha", "Sales", "09:30", "18:30", "2026-02-01"),
        ("EMP0004", "Rohit", "Sales", "09:30", "18:30", "2026-03-01"),
        ("EMP0005", "Isha", "Sales", "22:00", "06:00", "2026-03-01"),   # overnight
        ("EMP0006", "Zed", "Ops", "09:30", "18:30", "2026-01-01"),      # never has logs
        ("EMP0007", "Future", "Eng", "09:30", "18:30", "2026-08-03"),   # joins after the month
        ("EMP0008", "Tie", "Sales", "09:30", "18:30", "2026-01-01"),
    ]
    m.db.employees.insert_many([
        {"emp_code": c, "name": n, "email": f"{n}@x.com", "department": d, "shift_start": s, "shift_end": e,
         "joined_on": j, "created_at": m.now_millis()} for c, n, d, s, e, j in emps])
    rnd = random.Random(7)
    docs = []
    for c, n, d, s, e, j in emps:
        if c in ("EMP0006", "EMP0008"):
            continue
        emp = m.db.employees.find_one({"emp_code": c})
        day = FIRST
        while day <= LAST:
            if day.isoformat() == "2026-07-15" and c == "EMP0001":   # a weekday with no record at all
                day += timedelta(days=1)
                continue
            if rnd.random() < (0.85 if day.weekday() < 5 else 0.25):
                status = rnd.choices(["PRESENT", "WFH", "ON_DUTY", "LEAVE", "ABSENT"], [6, 2, 1, 1, 1])[0]
                if status in PRES:
                    base = m._day_at(day.isoformat(), s) + timedelta(minutes=rnd.randint(-20, 90), seconds=rnd.randint(0, 59))
                    p_in = m.from_ms(m.to_ms(base))
                    p_out = p_in + timedelta(minutes=rnd.randint(150, 640), seconds=rnd.randint(0, 59))
                    rec = {"emp_code": c, "date": day.isoformat(), "status": status, "punch_in": p_in, "punch_out": p_out,
                           "history": []}
                    rec.update(m.derive(emp, day.isoformat(), status, p_in, p_out))
                    if rnd.random() < 0.1:
                        del rec["history"]  # legacy shapes
                else:
                    rec = {"emp_code": c, "date": day.isoformat(), "status": status, "punch_in": None, "punch_out": None,
                           "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}
                docs.append(rec)
            day += timedelta(days=1)
    # ties: EMP0008 gets exactly the same late total as EMP0003 (and 0004 gets another tie group)
    totals = {}
    for r in docs:
        totals[r["emp_code"]] = totals.get(r["emp_code"], 0) + (r.get("late_minutes") or 0)
    docs.append({"emp_code": "EMP0008", "date": "2026-07-06", "status": "PRESENT", "punch_in": m.from_ms(1783312500000),
                 "punch_out": None, "work_hours": None, "late_minutes": totals["EMP0003"], "overtime_minutes": 0,
                 "half_day": False})
    docs.append({"emp_code": "EMP9999", "date": "2026-07-06", "status": "PRESENT", "punch_in": m.from_ms(1783312500000),
                 "punch_out": None, "work_hours": None, "late_minutes": 999, "overtime_minutes": 0})
    m.db.attendance_logs.insert_many(docs)


# ------------------------------ reference implementation ------------------------------ #
def load():
    emps = {e["emp_code"]: e for e in m.db.employees.find()}
    logs = list(m.db.attendance_logs.find())
    return emps, logs


def present_val(r, weekdays_only):
    if r["status"] not in PRES:
        return Decimal(0)
    if weekdays_only and date.fromisoformat(r["date"]).weekday() >= 5:
        return Decimal(0)
    return Decimal("0.5") if r.get("half_day") else Decimal(1)


def ref_monthly(code):
    emps, logs = load()
    rs = [r for r in logs if r["emp_code"] == code and MONTH + "-01" <= r["date"] <= MONTH + "-31"]
    start = max(FIRST, date.fromisoformat(emps[code]["joined_on"]))
    wd = sum(1 for i in range((LAST - start).days + 1) if (start + timedelta(days=i)).weekday() < 5) if start <= LAST else 0
    pres = sum(present_val(r, True) for r in rs)
    return {"emp_code": code, "month": MONTH, "working_days": wd, "present_days": m.num(pres),
            "leave_days": sum(r["status"] == "LEAVE" for r in rs),
            "late_count": sum((r.get("late_minutes") or 0) > 0 for r in rs),
            "total_late_minutes": sum(r.get("late_minutes") or 0 for r in rs),
            "total_overtime_minutes": sum(r.get("overtime_minutes") or 0 for r in rs),
            "attendance_pct": hu(pres / wd * 100, 2) if wd else None}


def ref_summary(dep=None):
    emps, logs = load()
    out = []
    for d in sorted({e["department"] for e in emps.values()}):
        if dep and d != dep:
            continue
        mem = [e for e in emps.values() if e["department"] == d and e["joined_on"] <= LAST.isoformat()]
        if not mem:
            continue
        codes = {e["emp_code"] for e in mem}
        rs = [r for r in logs if r["emp_code"] in codes and MONTH + "-01" <= r["date"] <= MONTH + "-31"]
        whs = [Decimal(repr(r["work_hours"])) for r in rs if r["status"] in PRES and r.get("work_hours") is not None]
        out.append({"department": d, "headcount": len(mem), "present_days": m.num(sum(present_val(r, True) for r in rs)),
                    "avg_work_hours": hu(sum(whs) / len(whs), 2) if whs else None,
                    "late_count": sum((r.get("late_minutes") or 0) > 0 for r in rs),
                    "total_late_minutes": sum(r.get("late_minutes") or 0 for r in rs),
                    "leave_count": sum(r["status"] == "LEAVE" for r in rs),
                    "on_duty_count": sum(r["status"] == "ON_DUTY" for r in rs)})
    return {"month": MONTH, "items": out}


def ref_leaderboard(limit, dep=None):
    emps, logs = load()
    tot = {}
    for r in logs:
        if r["emp_code"] in emps and MONTH + "-01" <= r["date"] <= MONTH + "-31":
            t = tot.setdefault(r["emp_code"], [0, 0])
            t[0] += r.get("late_minutes") or 0
            t[1] += (r.get("late_minutes") or 0) > 0
    rows = [(c, t) for c, t in tot.items() if t[0] > 0 and (not dep or emps[c]["department"] == dep)]
    rows.sort(key=lambda x: (-x[1][0], x[0]))
    items = []
    for c, t in rows:
        rank = 1 + sum(1 for _, u in rows if u[0] > t[0])  # standard competition ranking
        if rank <= limit:
            items.append({"rank": rank, "emp_code": c, "name": emps[c]["name"], "department": emps[c]["department"],
                          "total_late_minutes": t[0], "late_count": t[1]})
    return {"month": MONTH, "items": items}


def ref_trend(dep, d_from, d_to):
    emps, logs = load()
    mem = {c for c, e in emps.items() if e["department"] == dep}
    rows, rates = [], []
    day = d_from
    while day <= d_to:
        ds = day.isoformat()
        rs = [r for r in logs if r["emp_code"] in mem and r["date"] == ds]
        head = sum(1 for c in mem if emps[c]["joined_on"] <= ds)
        wd = day.weekday() < 5
        pc = sum(present_val(r, False) for r in rs)
        rate = (pc / Decimal(head)) if (wd and head > 0) else None
        rates.append(rate)
        window = [x for x in rates[-7:] if x is not None]
        rows.append({"date": ds, "is_working_day": wd, "headcount": head, "present_count": m.num(pc),
                     "late_count": sum((r.get("late_minutes") or 0) > 0 for r in rs),
                     "attendance_rate": None if rate is None else hu(rate, 4),
                     "moving_avg_7d": hu(sum(window) / len(window), 4) if window else None})
        day += timedelta(days=1)
    return {"department": dep, "items": rows}


# ------------------------------------ checks ------------------------------------ #
FAILS = 0


def check(name, got, want):
    global FAILS
    if got == want:
        print(f"ok    {name}")
        return
    FAILS += 1
    print(f"FAIL  {name}")
    if isinstance(got, dict) and isinstance(want, dict) and "items" in got:
        for g, w in zip(got["items"], want["items"]):
            if g != w:
                print("   got ", g, "\n   want", w)
                break
        if len(got["items"]) != len(want["items"]):
            print("   row counts:", len(got["items"]), len(want["items"]))
    else:
        print("   got ", got, "\n   want", want)


def main():
    seed()
    c = TestClient(m.app)
    for code in ("EMP0001", "EMP0002", "EMP0006", "EMP0007"):
        check(f"monthly {code}", c.get(f"/analytics/employees/{code}/monthly", params={"month": MONTH}).json(), ref_monthly(code))
    check("summary", c.get("/analytics/departments/summary", params={"month": MONTH}).json(), ref_summary())
    check("summary Sales", c.get("/analytics/departments/summary", params={"month": MONTH, "department": "Sales"}).json(), ref_summary("Sales"))
    for lim in (1, 2, 3, 10):
        check(f"leaderboard limit={lim}", c.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": lim}).json(), ref_leaderboard(lim))
    check("leaderboard Sales", c.get("/analytics/leaderboard/late", params={"month": MONTH, "department": "Sales"}).json(), ref_leaderboard(10, "Sales"))
    for dep in ("Eng", "Sales", "Ops"):
        r = c.get(f"/analytics/departments/{dep}/trend", params={"from": "2026-06-28", "to": "2026-08-05"})
        check(f"trend {dep}", r.json(), ref_trend(dep, date(2026, 6, 28), date(2026, 8, 5)))
    assert c.get("/analytics/departments/Nope/trend", params={"from": "2026-07-01", "to": "2026-07-05"}).status_code == 404
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-07-05", "to": "2026-07-01"}).status_code == 422
    assert c.get("/analytics/departments/Eng/trend", params={"from": "2026-01-01", "to": "2026-06-01"}).status_code == 422

    # explain: IXSCAN everywhere, never COLLSCAN
    calls = {
        "attendance_list": {}, "attendance_list ": {"emp_code": "EMP0001", "date_from": "2026-07-01"},
        "employee_monthly": {"emp_code": "EMP0001", "month": MONTH}, "department_summary": {"month": MONTH},
        "late_leaderboard": {"month": MONTH}, "department_trend": {"department": "Eng", "from": "2026-07-01", "to": "2026-07-31"},
    }
    for name, params in calls.items():
        r = c.get(f"/admin/explain/{name.strip()}", params=params)
        s = json.dumps(r.json()) if r.status_code == 200 else ""
        check(f"explain {name.strip()} {params or ''}", (r.status_code, "IXSCAN" in s, "COLLSCAN" in s), (200, True, False))
    assert c.get("/admin/explain/employee_monthly").status_code == 422

    # concurrency: 20 simultaneous punch-ins -> exactly one 201
    def hit(_):
        return TestClient(m.app).post("/attendance/punch-in", json={"emp_code": "EMP0006", "punched_at": 1783312500000}).status_code
    with ThreadPoolExecutor(20) as ex:
        codes = sorted(ex.map(hit, range(20)))
    check("20 parallel punch-ins", (codes.count(201), codes.count(409)), (1, 19))
    def out(_):
        return TestClient(m.app).post("/attendance/punch-out", json={"emp_code": "EMP0006", "punched_at": 1783312500000 + 9 * 3600_000}).status_code
    with ThreadPoolExecutor(20) as ex:
        codes = sorted(ex.map(out, range(20)))
    check("20 parallel punch-outs", (codes.count(200), codes.count(409)), (1, 19))
    def patch(i):
        return TestClient(m.app).patch("/attendance/EMP0006/2026-07-06",
                                       json={"reason": "parallel fix", "regularized_by": f"u{i}", "punch_in": 1783312500000 + (i + 1) * 60_000}).status_code
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(patch, range(10)))
    hist = len(m.db.attendance_logs.find_one({"emp_code": "EMP0006", "date": "2026-07-06"})["history"])
    check("parallel PATCH: history == number of 200s", hist, codes.count(200))

    print("\nALL GOOD" if FAILS == 0 else f"\n{FAILS} check(s) FAILED")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
