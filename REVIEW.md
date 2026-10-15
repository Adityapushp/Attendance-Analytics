# REVIEW.md

I read the starter `app/main.py` like a pull request. Everything below is fixed in my version.

| # | Where | What is wrong | How you would notice | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It compares the punch-in with the shift start using the server's clock, not IST. | A punch-in at 10:05 IST on a UTC server is compared with 09:30 UTC, so it shows hundreds of late minutes. | Convert to IST first, and build the shift start as an IST time on the record's date. |
| 2 | `compute_late_minutes` | It rounds down to whole minutes *before* checking "more than 10", so a punch-in at 09:40:01 gives 10, which is not more than 10, and ends as 0. The rule says it should be 10. Overnight shifts and sub-second values are also ignored. | Shift 09:30, punch-in 09:40:01 returns 0, not 10. | Compare in seconds (`> 600`), then divide by 60. Overnight dates are handled in one helper, `attendance_date`. |
| 3 | `compute_work_hours` | Python's `round()` on a float is not half-up. | 09:30:00 to 18:30:18 is exactly 9.005 hours. `round` gives 9.0, the rule wants 9.01. | Use `Decimal` and `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | It subtracts an IST-aware time from a naive one, which raises `TypeError` (a 500). It also has no 30-minute minimum, and overnight shifts end on the wrong day. | Any punch-out. A 10-minute overtime should be 0, but it returns 10. | Use aware datetimes everywhere, add the 30-minute minimum, and move overnight shift ends to the next day. |
| 5 | `MongoClient(...)` at the top | By default PyMongo returns naive datetimes, so time zones depend on the machine. | Mixing naive and aware times crashes or shifts dates. | Use `tz_aware=True` and convert only at the API edge. |
| 6 | `EmployeeIn` | There is no validation: bad `emp_code`, bad email, shift times like `25:99`, equal start and end, impossible dates. | POST with `shift_start: "25:99"` returns 201, then punch-in crashes later. | Add patterns, a real-date check, and a rule that start must differ from end. |
| 7 | `PunchInIn` | `punched_at` accepts strings, floats and seconds-sized numbers. `status` accepts anything, including `ABSENT`. | `punched_at: 1783312500` (seconds) is accepted and stored as 1970. | Use a strict integer in the allowed range, and limit `status` to PRESENT, WFH and ON_DUTY. |
| 8 | `create_employee` | It looks first, then inserts, with no unique index, so duplicates can slip in. It also uses naive local time and returns `created_at` as a datetime instead of epoch milliseconds. | Two parallel POSTs with the same code both return 201. | Add a unique index, catch `DuplicateKeyError` for 409, use UTC, and return epoch milliseconds. |
| 9 | `list_employees` | `skip = page * page_size` skips the whole first page. `total` ignores the department filter. There is no sort, `page=0` gives a negative skip (500), and `page_size` has no limit. | Page 1 starts at the 21st employee. | Use `(page - 1) * page_size`, count the filtered set, sort by `emp_code`, and add bounds on both parameters. |
| 10 | `punch_in` | For an unknown employee the lookup returns `None` and the code crashes (500). | POST with an unknown `emp_code`. | Return 404. |
| 11 | `punch_in` | The date comes from server-local time, not IST. Fractions of a second are kept, `punched_at: 0` is treated as missing, and overnight shifts are ignored. | A punch-in at 20:00 UTC is already the next day in IST, but gets the wrong date. | One helper, `attendance_date`, applies the IST and overnight rules. Times are truncated to seconds. |
| 12 | `punch_in` | Another look-then-insert race. | 20 parallel requests return several 201s. | Unique `(emp_code, date)` index, then insert and catch the duplicate error. |
| 13 | `punch_in` response | It leaks Mongo's `_id` as `id` and returns raw datetimes. | The response contains `id`. | A `record_out` function returns exactly the contract fields. |
| 14 | `list_attendance` | It loads the whole collection into memory and sorts in Python. | Slow and memory-heavy at 100k records. | Use `count_documents` plus `find().sort().skip().limit()`, backed by an index. |
| 15 | `list_attendance` | It sorts by date only, does not validate dates or `status`, returns empty instead of 422 when `date_from` is after `date_to`, breaks on `page=0`, and returns old records without `history` or `half_day` as they are. | `date_from=2026-13-45`, or `page=0`. | Validate the inputs, sort by `(date desc, emp_code)`, and fill defaults for missing fields. |
| 16 | `/health` | It always says ok, even when MongoDB is down. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 if it fails. |
| 17 | startup | No indexes are created. | `explain` shows a collection scan. | Create all indexes at startup. They are named and safe to repeat. |

## Looked at and decided it was fine
- `load_dotenv()`: it does not replace variables already set in the environment, which is what the contract needs.
- The `IST` value: UTC+05:30 is correct.
- Removing `_id` in the create response: this is right, the API must not expose it.
