"""CRUD-level API tests on an in-memory fake (mongomock). Analytics pipelines need a real MongoDB:
use tests/verify_analytics.py for those."""
import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as m


@pytest.fixture()
def api():
    fake = mongomock.MongoClient(tz_aware=True)
    m.db = fake["t"]
    m.client = fake
    m._indexes_ready = False
    assert m.ensure_indexes()
    return TestClient(m.app)


def emp(api, code="EMP0001", **kw):
    body = {"emp_code": code, "name": "Asha", "email": "a@x.com", "department": "Eng", "joined_on": "2026-01-05"}
    body.update(kw)
    return api.post("/employees", json=body)


def ms(h, mi, s=0, day=6):  # 2026-07-<day> HH:MM:SS IST -> epoch ms
    from datetime import datetime
    return int(datetime(2026, 7, day, h, mi, s, tzinfo=m.IST).timestamp()) * 1000


def test_employee_create_validation_and_duplicates(api):
    r = emp(api)
    assert r.status_code == 201 and "_id" not in r.json() and isinstance(r.json()["created_at"], int)
    assert emp(api).status_code == 409
    assert emp(api, "BAD1").status_code == 422
    assert emp(api, "EMP0002", shift_start="09:00", shift_end="09:00").status_code == 422
    assert emp(api, "EMP0003", joined_on="2026-02-30").status_code == 422
    assert emp(api, "EMP0004", shift_start="25:00").status_code == 422


def test_employee_list_pagination_and_total(api):
    for i, d in enumerate(["Eng", "Eng", "Sales"], start=1):
        emp(api, f"EMP000{i}", department=d)
    r = api.get("/employees", params={"department": "Eng", "page_size": 1, "page": 2}).json()
    assert r["total"] == 2 and [e["emp_code"] for e in r["items"]] == ["EMP0002"]
    assert api.get("/employees", params={"page": 0}).status_code == 422
    assert api.get("/employees", params={"page_size": 101}).status_code == 422


def test_punch_in_out_flow(api):
    emp(api)
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(9, 40, 1)})
    assert r.status_code == 201
    j = r.json()
    assert j["date"] == "2026-07-06" and j["late_minutes"] == 10 and j["history"] == [] and "id" not in j
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(9, 50)}).status_code == 409
    assert api.post("/attendance/punch-in", json={"emp_code": "NOPE"}).status_code == 404
    out = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(19, 10)})
    assert out.status_code == 200
    o = out.json()
    assert o["work_hours"] == 9.5 and o["overtime_minutes"] == 40 and o["half_day"] is False
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(19, 20)}).status_code == 409


def test_punch_validation(api):
    emp(api)
    for bad in (1783312500, 1783312500000.5, "1783312500000", True):
        r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad})
        assert r.status_code == 422, bad
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(9, 30)})
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(9, 30)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(9, 30, day=8)}).status_code == 422
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(8, 0)}).status_code == 404


def test_overnight_punch_out_across_midnight(api):
    emp(api, "EMP0005", shift_start="22:00", shift_end="06:00")
    api.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms(21, 55)})
    r = api.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ms(6, 40, day=7)})
    assert r.status_code == 200 and r.json()["overtime_minutes"] == 40 and r.json()["work_hours"] == 8.75


def test_regularize_history_and_rules(api):
    emp(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(10, 5)})
    api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(18, 30)})
    base = {"reason": "biometric glitch", "regularized_by": "hr.admin"}
    r = api.patch("/attendance/EMP0001/2026-07-06", json={**base, "punch_in": ms(9, 28)})
    assert r.status_code == 200
    j = r.json()
    assert j["late_minutes"] == 0 and len(j["history"]) == 1
    ch = j["history"][0]["changes"]
    assert set(ch) == {"punch_in", "work_hours", "late_minutes"}
    assert ch["late_minutes"] == {"from": 35, "to": 0} and ch["punch_in"]["from"] == ms(10, 5)
    assert api.patch("/attendance/EMP0001/2026-07-06", json={**base, "punch_in": ms(9, 28)}).status_code == 422  # no-op
    assert api.patch("/attendance/EMP0001/2026-07-06", json={**base, "punch_in": ms(9, 0, day=7)}).status_code == 422  # wrong date
    assert api.patch("/attendance/EMP0001/2026-07-06", json={**base, "status": "LEAVE", "punch_in": ms(9, 0)}).status_code == 422
    assert api.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "LEAVE"}).status_code == 404
    r = api.patch("/attendance/EMP0001/2026-07-06", json={**base, "status": "LEAVE"})
    assert r.status_code == 200 and r.json()["punch_in"] is None and len(r.json()["history"]) == 2
    assert r.json()["work_hours"] is None and r.json()["late_minutes"] == 0
    # presence status without any punch_in is rejected; with one it is accepted
    assert api.patch("/attendance/EMP0001/2026-07-06", json={**base, "status": "PRESENT"}).status_code == 422
    r = api.patch("/attendance/EMP0001/2026-07-06", json={**base, "status": "WFH", "punch_in": ms(9, 30)})
    assert r.status_code == 200 and r.json()["status"] == "WFH" and len(r.json()["history"]) == 3


def test_attendance_list_filters_sort_paging(api):
    emp(api, "EMP0001"); emp(api, "EMP0002")
    for code in ("EMP0002", "EMP0001"):
        for day in (6, 7):
            api.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ms(9, 30, day=day)})
    r = api.get("/attendance", params={"page_size": 3}).json()
    assert r["total"] == 4
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [
        ("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002"), ("2026-07-06", "EMP0001")]
    assert api.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-01"}).status_code == 422
    assert api.get("/attendance", params={"date_from": "2026-13-01"}).status_code == 422
    assert api.get("/attendance", params={"status": "NOPE"}).status_code == 422
    assert api.get("/attendance", params={"emp_code": "EMP0002", "date_from": "2026-07-07"}).json()["total"] == 1
