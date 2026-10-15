"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded; real environment variables win)

Layout of this single file
    1. configuration + constants
    2. small helpers (time, rounding, validation, serialisation)
    3. business rules R1-R5 (late / overtime / work hours / attendance date)
    4. pydantic models
    5. indexes + startup
    6. employees, attendance (punch-in / punch-out / list / regularize)
    7. analytics (aggregation pipelines) and the explain endpoint
"""
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import (
    AutoReconnect,
    DuplicateKeyError,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
)

# --------------------------------------------------------------------------- #
# 1. Configuration + constants
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override variables that are already set in the environment
log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns timezone-aware (UTC) datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
# strict=True: no "123" strings, no floats, no booleans; the range rejects seconds-looking values
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]

DATE_RE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
MONTH_RE = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HM_RE = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
EMP_RE = r"^EMP[0-9]{4,6}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

GRACE_SECONDS = 600  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_LIMIT = Decimal("4.50")  # R5
MAX_SHIFT_SPAN = timedelta(hours=24)


# --------------------------------------------------------------------------- #
# 2. Helpers
# --------------------------------------------------------------------------- #
def unprocessable(loc: list, msg: str):
    """Raise a 422 shaped like FastAPI's own validation errors."""
    raise HTTPException(
        status_code=422,
        detail=[{"type": "value_error", "loc": loc, "msg": msg, "input": None}],
    )


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def to_ms(dt: datetime) -> int:
    """Datetime -> epoch milliseconds (naive datetimes are treated as UTC)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def now_millis() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=(n.microsecond // 1000) * 1000)  # BSON keeps milliseconds


def to_decimal(x: Any) -> Decimal:
    if isinstance(x, Decimal128):
        return x.to_decimal()
    if isinstance(x, Decimal):
        return x
    return Decimal(repr(x))


def round_half_up(x: Any, places: int) -> float:
    """R8: half-up rounding (Python's round() and MongoDB's $round are banker's)."""
    q = Decimal(1).scaleb(-places)
    return float(to_decimal(x).quantize(q, rounding=ROUND_HALF_UP))


def num(x: Any):
    """Whole numbers as int, others as float (present_days can be 0.5-stepped)."""
    if x is None:
        return None
    f = float(to_decimal(x))
    return int(f) if f == int(f) else f


def parse_date(value: str, loc: list) -> date:
    try:
        if len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, "not a valid calendar date (YYYY-MM-DD)")


def month_bounds(month: str) -> tuple[str, str, date, date]:
    try:
        y, m = int(month[:4]), int(month[5:7])
        first = date(y, m, 1)
    except ValueError:
        unprocessable(["query", "month"], "not a valid month (YYYY-MM)")
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    last = nxt - timedelta(days=1)
    return first.isoformat(), last.isoformat(), first, last


def weekend_dates(first: date, last: date) -> list[str]:
    out, d = [], first
    while d <= last:
        if d.weekday() >= 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def count_weekdays(start: date, end: date) -> int:
    """Mon-Fri days in [start, end] (R7). Pure calendar arithmetic, no data involved."""
    if start > end:
        return 0
    days = (end - start).days + 1
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rest):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 3. Business rules R1 - R5
# --------------------------------------------------------------------------- #
def _hm(s: str) -> tuple[int, int]:
    return int(s[:2]), int(s[3:5])


def is_overnight(emp: dict) -> bool:
    return _hm(emp["shift_end"]) <= _hm(emp["shift_start"])


def _day_at(date_str: str, hm: str, extra_days: int = 0) -> datetime:
    h, m = _hm(hm)
    d = date.fromisoformat(date_str) + timedelta(days=extra_days)
    return datetime.combine(d, dtime(h, m), tzinfo=IST)


def attendance_date(punch_in: datetime, emp: dict) -> str:
    """R1: IST calendar date of the punch-in; early punch-ins of an overnight shift belong to yesterday."""
    ist = aware(punch_in).astimezone(IST)
    d = ist.date()
    if is_overnight(emp):
        eh, em = _hm(emp["shift_end"])
        if ist.time() < dtime(eh, em):
            d -= timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(punch_in: datetime, date_str: str, emp: dict) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    delta = int((aware(punch_in) - _day_at(date_str, emp["shift_start"])).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, date_str: str, emp: dict) -> int:
    """R3: whole minutes past shift end, counted only if >= 30. Overnight shifts end the next day."""
    end = _day_at(date_str, emp["shift_end"], 1 if is_overnight(emp) else 0)
    minutes = int((aware(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600, 2 decimals, half-up."""
    secs = int((aware(punch_out) - aware(punch_in)).total_seconds())
    return (Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def derive(emp: dict, date_str: str, status: str, punch_in, punch_out) -> dict:
    """All derived fields for one record (R2-R5); the table in DATA_MODEL.md section 3."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, date_str, emp)
    if punch_out is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    wh = compute_work_hours(punch_in, punch_out)
    return {
        "work_hours": float(wh),
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out, date_str, emp),
        "half_day": wh < HALF_DAY_LIMIT,
    }


# --------------------------------------------------------------------------- #
# Serialisation (BSON -> API)
# --------------------------------------------------------------------------- #
def _api_value(v: Any):
    return to_ms(v) if isinstance(v, datetime) else v


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc["created_at"]) if doc.get("created_at") else None,
    }


def record_out(doc: dict) -> dict:
    history = []
    for h in doc.get("history") or []:
        history.append(
            {
                "at": _api_value(h.get("at")),
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": {
                    k: {"from": _api_value(v.get("from")), "to": _api_value(v.get("to"))}
                    for k, v in (h.get("changes") or {}).items()
                },
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _api_value(doc.get("punch_in")),
        "punch_out": _api_value(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# 4. Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=EMP_RE)
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=EMAIL_RE)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HM_RE)
    shift_end: str = Field(default="18:30", pattern=HM_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("name", "department")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# 5. Indexes + startup
# --------------------------------------------------------------------------- #
INDEXES = [
    # natural key + the guarantee behind race-safe punch-in; also serves per-employee ranges
    ("attendance_logs", [("emp_code", 1), ("date", 1)], {"unique": True, "name": "uniq_emp_date"}),
    # list sort order (date desc, emp_code asc), date ranges, and the month-wide analytics
    ("attendance_logs", [("date", -1), ("emp_code", 1)], {"name": "date_emp"}),
    # unique emp_code: duplicate-safe create, lookups from logs
    ("employees", [("emp_code", 1)], {"unique": True, "name": "uniq_emp_code"}),
    # department filter + emp_code order (list, trend, headcount)
    ("employees", [("department", 1), ("emp_code", 1)], {"name": "dept_emp"}),
    # headcount by joined_on (department summary without a department filter)
    ("employees", [("joined_on", 1)], {"name": "joined_on"}),
]
_indexes_ready = False


def ensure_indexes() -> bool:
    """Idempotent. Returns False only if the database could not be reached."""
    global _indexes_ready
    if _indexes_ready:
        return True
    try:
        for coll, keys, opts in INDEXES:
            try:
                db[coll].create_index(keys, **opts)
            except OperationFailure as exc:  # e.g. existing duplicates; do not crash the app
                log.error("could not create index %s on %s: %s", opts.get("name"), coll, exc)
        _indexes_ready = True
    except (ServerSelectionTimeoutError, AutoReconnect) as exc:
        log.warning("MongoDB not reachable yet: %s", exc)
    return _indexes_ready


@asynccontextmanager
async def lifespan(_: FastAPI):
    deadline = time.monotonic() + 12
    while not ensure_indexes() and time.monotonic() < deadline:
        time.sleep(1)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    log.exception("database error")
    return JSONResponse(status_code=503, content={"detail": "database error"})


# --------------------------------------------------------------------------- #
# 6a. System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_millis()
    try:
        db.employees.insert_one(doc)  # the unique index decides, not a prior find()
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    items = []
    if skip < total:
        cur = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
        items = [employee_out(d) for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 6b. Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    d = attendance_date(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, d, emp),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one racer wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_seconds()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = aware(rec["punch_in"])
    if ts <= p_in:
        unprocessable(["body", "punched_at"], "punched_at must be after the punch-in")
    if ts - p_in > MAX_SHIFT_SPAN:
        unprocessable(["body", "punched_at"], "punch-out is more than 24 hours after the punch-in")
    fields = derive(emp, rec["date"], rec["status"], rec["punch_in"], ts)
    fields["punch_out"] = ts
    # Conditional update: succeeds only while the record is still open and unchanged.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out (or modified concurrently)")
    return record_out(updated)


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


def check_date_range(date_from: Optional[str], date_to: Optional[str], loc=("query", "date_from")):
    df = parse_date(date_from, list(loc)) if date_from else None
    dt = parse_date(date_to, ["query", "date_to"]) if date_to else None
    if df and dt and df > dt:
        unprocessable(list(loc), "date_from must not be after date_to")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    check_date_range(date_from, date_to)
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    items = []
    if skip < total:
        cur = db.attendance_logs.find(q).sort(ATTENDANCE_SORT).skip(skip).limit(page_size)
        items = [record_out(d) for d in cur]  # only one page is ever held in memory
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):  # noqa: A002 (path name is the contract)
    parse_date(date, ["path", "date"])
    for f in ("status", "punch_in", "punch_out"):
        if f in body.model_fields_set and getattr(body, f) is None:
            unprocessable(["body", f], "must not be null")
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or rec["status"]
    in_given, out_given = body.punch_in is not None, body.punch_out is not None

    if new_status in ("ABSENT", "LEAVE"):
        if in_given or out_given:
            unprocessable(["body"], f"{new_status} clears punch times; do not send punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = from_ms(body.punch_in) if in_given else old_in
        new_out = from_ms(body.punch_out) if out_given else old_out
        if new_in is None:
            unprocessable(["body", "punch_in"], f"status {new_status} requires a punch_in")
        if in_given and attendance_date(new_in, emp) != date:
            unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if (in_given or out_given) and new_out is not None:
            if aware(new_out) <= aware(new_in):
                unprocessable(["body", "punch_out"], "punch_out must be after punch_in")
            if aware(new_out) - aware(new_in) > MAX_SHIFT_SPAN:
                unprocessable(["body", "punch_out"], "punch_out is more than 24 hours after punch_in")

    def same_instant(a, b) -> bool:
        return (a is None and b is None) or (a is not None and b is not None and to_ms(a) == to_ms(b))

    if new_status == rec["status"] and same_instant(old_in, new_in) and same_instant(old_out, new_out):
        unprocessable(["body"], "request changes nothing")

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    new.update(derive(emp, date, new_status, new_in, new_out))
    old = {
        "status": rec["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day") or False),
    }
    changes = {}
    for k in ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"):
        a, b = old[k], new[k]
        changed = (not same_instant(a, b)) if k in ("punch_in", "punch_out") else a != b
        if changed:
            changes[k] = {"from": a, "to": b}

    entry = {"at": now_millis(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    n = len(rec.get("history") or [])
    # Optimistic guard: nothing may have been appended (history.<n> absent) or edited since we read it.
    guard = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        f"history.{n}": {"$exists": False},
    }
    update: dict = {"$set": new}
    if rec.get("history") is None and "history" in rec:
        update["$set"]["history"] = [entry]  # explicit null cannot be $push-ed to
    else:
        update["$push"] = {"history": entry}
    updated = db.attendance_logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 7. Analytics (all aggregation pipelines)
# --------------------------------------------------------------------------- #
# Expression builders. `r(field)` returns the reference to a field of "the log" in the current
# context: "$status" inside a $group over logs, "$$l.status" inside a $map over a logs array.
def _late(r):
    return {"$ifNull": [r("late_minutes"), 0]}


def _late_flag(r):
    return {"$cond": [{"$gt": [_late(r), 0]}, 1, 0]}


def _present_value(r, weekends: Optional[list]):
    """1 for a present-status record, 0.5 for a half day, 0 otherwise (R5, R6); Mon-Fri only if `weekends` given (R7)."""
    cond: Any = {"$in": [r("status"), list(PRESENCE)]}
    if weekends is not None:
        cond = {"$and": [cond, {"$not": [{"$in": [r("date"), weekends]}]}]}
    half = {"$eq": [{"$ifNull": [r("half_day"), False]}, True]}
    return {"$cond": [cond, {"$cond": [half, 0.5, 1]}, 0]}


def monthly_pipeline(emp_code: str, first: str, last: str, weekends: list) -> list:
    r = lambda f: "$" + f  # noqa: E731
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _present_value(r, weekends)},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": _late_flag(r)},
                "total_late_minutes": {"$sum": _late(r)},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def summary_pipeline(first: str, last: str, weekends: list, department: Optional[str]) -> list:
    """Starts from `employees` so people with zero logs are still counted (R9)."""
    match: dict = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    r = lambda f: "$$l." + f  # noqa: E731
    mapped = lambda expr: {"$map": {"input": "$logs", "as": "l", "in": expr}}  # noqa: E731
    count_where = lambda cond: {  # noqa: E731
        "$size": {"$filter": {"input": "$logs", "as": "l", "cond": cond}}
    }
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1,
                                  "late_minutes": 1, "work_hours": 1}},
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "p": {"$sum": mapped(_present_value(r, weekends))},
                "lc": count_where({"$gt": [_late(r), 0]}),
                "lt": {"$sum": mapped(_late(r))},
                "lv": count_where({"$eq": [r("status"), "LEAVE"]}),
                "od": count_where({"$eq": [r("status"), "ON_DUTY"]}),
                # exact decimals so the average can be rounded half-up without float noise
                "whv": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": "$logs",
                                "as": "l",
                                "cond": {"$and": [{"$in": [r("status"), list(PRESENCE)]},
                                                  {"$isNumber": r("work_hours")}]},
                            }
                        },
                        "as": "l",
                        "in": {"$toDecimal": r("work_hours")},
                    }
                },
            }
        },
        {"$addFields": {"whs": {"$sum": "$whv"}, "whn": {"$size": "$whv"}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$p"},
                "late_count": {"$sum": "$lc"},
                "total_late_minutes": {"$sum": "$lt"},
                "leave_count": {"$sum": "$lv"},
                "on_duty_count": {"$sum": "$od"},
                "wh_sum": {"$sum": "$whs"},
                "wh_n": {"$sum": "$whn"},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]) -> list:
    r = lambda f: "$" + f  # noqa: E731
    pipe: list = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": _late(r)}, "late_count": {"$sum": _late_flag(r)}}},
        {"$match": {"total": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department is not None:
        pipe.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipe += [
        # RANK() semantics = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking
        {"$sort": {"total": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total",
                "late_count": 1,
            }
        },
    ]
    return pipe


def department_codes(department: str) -> list:
    cur = db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})
    return [d["emp_code"] for d in cur]


def trend_pipeline(department: str, d_from: date, d_to: date, codes: list) -> list:
    """One row per calendar day, built inside the database ($range skeleton + window function)."""
    n_days = (d_to - d_from).days + 1
    from_dt = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    r = lambda f: "$" + f  # noqa: E731
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "joins": {"$push": "$joined_on"}}},
        # one uncorrelated lookup: per-day sums for the department's logs, via the (emp_code, date) index
        {
            "$lookup": {
                "from": "attendance_logs",
                "pipeline": [
                    {"$match": {"emp_code": {"$in": codes},
                                "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()}}},
                    {"$group": {"_id": "$date",
                                "present": {"$sum": _present_value(r, None)},
                                "late": {"$sum": _late_flag(r)}}},
                ],
                "as": "days",
            }
        },
        {"$project": {"_id": 0, "joins": 1, "days": 1, "offset": {"$range": [0, n_days]}}},
        {"$unwind": "$offset"},
        {"$addFields": {"dt": {"$add": [from_dt, {"$multiply": ["$offset", 86400000]}]}}},
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}},
                        "wd": {"$lte": [{"$isoDayOfWeek": "$dt"}, 5]}}},
        {
            "$addFields": {
                "headcount": {"$size": {"$filter": {"input": "$joins", "as": "j",
                                                     "cond": {"$lte": ["$$j", "$date"]}}}},
                "hit": {"$arrayElemAt": [{"$filter": {"input": "$days", "as": "d",
                                                       "cond": {"$eq": ["$$d._id", "$date"]}}}, 0]},
            }
        },
        {"$addFields": {"present_count": {"$ifNull": ["$hit.present", 0]},
                        "late_count": {"$ifNull": ["$hit.late", 0]}}},
        {
            "$addFields": {
                "rate": {
                    "$cond": [
                        {"$and": ["$wd", {"$gt": ["$headcount", 0]}]},
                        {"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]},
                        None,
                    ]
                }
            }
        },
        # mean of the non-null rates over this row and up to 6 rows before it, inside the range
        {"$setWindowFields": {"sortBy": {"date": 1},
                              "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "wd": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "rate": 1, "ma": 1}},
        {"$sort": {"date": 1}},
    ]


def trend_args(from_: str, to: str) -> tuple[date, date]:
    d_from = parse_date(from_, ["query", "from"])
    d_to = parse_date(to, ["query", "to"])
    if d_to < d_from:
        unprocessable(["query", "to"], "'to' must not be before 'from'")
    if (d_to - d_from).days + 1 > 92:
        unprocessable(["query", "to"], "range must not exceed 92 days")
    return d_from, d_to


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    first, last, fd, ld = month_bounds(month)
    emp = get_employee(emp_code)
    pipe = monthly_pipeline(emp_code, first, last, weekend_dates(fd, ld))
    row = next(iter(db.attendance_logs.aggregate(pipe)), {})
    try:
        joined = date.fromisoformat(emp["joined_on"])
    except (KeyError, ValueError):
        joined = fd
    working_days = count_weekdays(max(fd, joined), ld)
    present = to_decimal(row.get("present_days", 0))
    pct = None
    if working_days > 0:
        pct = round_half_up(present / Decimal(working_days) * 100, 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": num(present),
        "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    first, last, fd, ld = month_bounds(month)
    pipe = summary_pipeline(first, last, weekend_dates(fd, ld), department)
    items = []
    for row in db.employees.aggregate(pipe, allowDiskUse=True):
        n = int(row["wh_n"])
        avg = round_half_up(to_decimal(row["wh_sum"]) / Decimal(n), 2) if n else None
        items.append(
            {
                "department": row["_id"],
                "headcount": int(row["headcount"]),
                "present_days": num(row["present_days"]),
                "avg_work_hours": avg,
                "late_count": int(row["late_count"]),
                "total_late_minutes": int(row["total_late_minutes"]),
                "leave_count": int(row["leave_count"]),
                "on_duty_count": int(row["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last, _, _ = month_bounds(month)
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(first, last, limit, department), allowDiskUse=True)
    items = [
        {
            "rank": int(x["rank"]),
            "emp_code": x["emp_code"],
            "name": x["name"],
            "department": x["department"],
            "total_late_minutes": int(x["total_late_minutes"]),
            "late_count": int(x["late_count"]),
        }
        for x in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_RE),
    to: str = Query(..., pattern=DATE_RE),
):
    d_from, d_to = trend_args(from_, to)
    codes = department_codes(department)
    if not codes:
        raise HTTPException(404, "unknown department")
    rows = db.employees.aggregate(trend_pipeline(department, d_from, d_to, codes), allowDiskUse=True)
    items = [
        {
            "date": x["date"],
            "is_working_day": bool(x["wd"]),
            "headcount": int(x["headcount"]),
            "present_count": num(x["present_count"]),
            "late_count": int(x["late_count"]),
            "attendance_rate": None if x["rate"] is None else round_half_up(x["rate"], 4),
            "moving_avg_7d": None if x.get("ma") is None else round_half_up(x["ma"], 4),
        }
        for x in rows
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain: builds the same query/pipeline as the endpoint and asks MongoDB how it would run it
# --------------------------------------------------------------------------- #
ExplainTarget = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]


def _need(value, name: str):
    if value is None:
        unprocessable(["query", name], "field required")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    to: Optional[str] = Query(None, pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        check_date_range(date_from, date_to)
        coll = "attendance_logs"
        cmd: dict = {
            "find": coll,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        code = _need(emp_code, "emp_code")
        first, last, fd, ld = month_bounds(_need(month, "month"))
        coll = "attendance_logs"
        cmd = {"aggregate": coll, "pipeline": monthly_pipeline(code, first, last, weekend_dates(fd, ld)),
               "cursor": {}}
    elif endpoint == "department_summary":
        first, last, fd, ld = month_bounds(_need(month, "month"))
        coll = "employees"
        cmd = {"aggregate": coll, "pipeline": summary_pipeline(first, last, weekend_dates(fd, ld), department),
               "cursor": {}}
    elif endpoint == "late_leaderboard":
        first, last, _, _ = month_bounds(_need(month, "month"))
        coll = "attendance_logs"
        cmd = {"aggregate": coll, "pipeline": leaderboard_pipeline(first, last, limit, department),
               "cursor": {}}
    else:  # department_trend
        dept = _need(department, "department")
        d_from, d_to = trend_args(_need(from_, "from"), _need(to, "to"))
        coll = "employees"
        cmd = {"aggregate": coll, "pipeline": trend_pipeline(dept, d_from, d_to, department_codes(dept)),
               "cursor": {}}

    result = db.command({"explain": cmd, "verbosity": "executionStats"})
    plain = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": coll, "explain": plain}
