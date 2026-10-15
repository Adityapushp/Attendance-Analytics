# REVIEW.md

Defects found in the starter `app/main.py`, all fixed in this version.

| # | Where (function / line) | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Replaces the hour/minute on a naive server-time value, so the shift start is compared in server time, not IST (R1). | Punch-in 10:05 IST on a UTC server compares against 09:30 UTC and reports hundreds of late minutes. | Everything is converted to IST first; shift start is built as an IST datetime on the record's date. |
| 2 | `compute_late_minutes` | Floors to whole minutes **before** testing `> 10`, so 09:40:01 gives 10, which is not `> 10`, so 0. R2 says 10. Also no overnight shifts and sub-second values are not truncated. | Punch-in 09:40:01 on shift 09:30 returns 0 instead of 10. | Compare seconds (`> 600`), then floor: `delta // 60`. Overnight handled in `attendance_date`. |
| 3 | `compute_work_hours` | `round(x, 2)` on a float: not half-up (R4). | 09:30:00 to 18:30:18 is exactly 9.005 h; `round` gives 9.0, R4 says 9.01. | `Decimal` division and `quantize(..., ROUND_HALF_UP)`. |
| 4 | `compute_overtime` | Subtracts an aware IST datetime from a naive one, so `TypeError` (500). No 30-minute minimum (R3). Overnight shift end is not moved to the next day. | Any punch-out with default PyMongo datetimes; punch-out 18:40 returns 10 instead of 0. | Aware datetimes everywhere (`tz_aware=True`), 30-minute threshold, next-day end for overnight shifts. |
| 5 | module level `MongoClient(...)` | Default returns naive datetimes; timezone handling silently depends on the server clock. | Mixing with aware values raises; dates shift on non-UTC servers. | `tz_aware=True`; all conversions go through `from_ms` / `to_ms`. |
| 6 | `EmployeeIn` | No validation at all: bad `emp_code`, email, shift times like `25:99`, `shift_start == shift_end`, impossible `joined_on`. | POST `shift_start: "25:99"` returns 201; later punch-ins crash. | Field patterns, blank checks, real-date check, model validator for equal shifts. |
| 7 | `PunchInIn` | `punched_at: Optional[int]` accepts strings/floats and seconds-looking values; `status: str` accepts anything, including `ABSENT`. | `punched_at: 1783312500` (seconds) is accepted and stored as 1970. | Strict int with the contract's range; `status` is a `Literal` of the three presence values. |
| 8 | `create_employee` | Check-then-insert race, no unique index; `datetime.now()` is naive server time; `created_at` is returned as a datetime, not epoch ms. | Two parallel POSTs both return 201. | Unique index + `DuplicateKeyError` to 409; UTC time; epoch ms in the response. |
| 9 | `list_employees` | `skip = page * page_size` skips the first page; `total` ignores the department filter; no sort; `page=0` gives a negative skip (500); no `page_size` cap. | Page 1 starts at the 21st employee; `total` is wrong with `department=...`. | `(page - 1) * page_size`, filtered count, sort by `emp_code`, `Query` bounds (R10). |
| 10 | `punch_in` | Unknown employee: `emp` is `None`, so `TypeError` (500) instead of 404. | POST with an unknown `emp_code`. | `get_employee` raises 404. |
| 11 | `punch_in` | Date comes from `datetime.fromtimestamp` in server-local time, not IST; sub-seconds not truncated; `if body.punched_at` treats 0 as missing; no overnight rule. | Punch-in at 20:00 UTC (01:30 IST next day) is filed under the wrong date. | `attendance_date()` implements R1; instants truncated to seconds; `is not None` checks. |
| 12 | `punch_in` | Check-then-insert race again. | 20 parallel identical requests give several 201s. | Unique `(emp_code, date)` index; insert and catch `DuplicateKeyError`. |
| 13 | `punch_in` response | Leaks `_id` as `id`, returns raw datetimes. | Response contains `id`. | `record_out()` returns the contract shape with epoch ms. |
| 14 | `list_attendance` | `list(find(q))` loads the whole collection and sorts in Python; no indexes. | Slow and memory-heavy at 100k records. | `count_documents` + `find().sort().skip().limit()` backed by indexes. |
| 15 | `list_attendance` | Sorts by `date` only (no `emp_code` tie-break); dates and `status` unvalidated; `date_from > date_to` returns empty instead of 422; `page=0` slices with negative indexes; leaks `id`; legacy rows without `history`/`half_day` are returned as-is. | `date_from=2026-13-45`; `page=0`. | Validated params, sort `(date desc, emp_code)`, defaults for missing fields. |
| 16 | `/health` | Always returns ok, even when MongoDB is down. | Stop MongoDB, call `/health`. | Pings MongoDB; 503 on failure. |
| 17 | startup | No indexes are created. | `explain` shows COLLSCAN. | `ensure_indexes()` at startup, idempotent and named. |

**Looked at and judged fine:** `load_dotenv()` (does not override real environment variables, as the contract needs); the `IST` constant (UTC+05:30 is right); removing `_id` from `create_employee`'s response; the `/health` route path itself.
