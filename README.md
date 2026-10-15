# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # local: docker run -p 27017:27017 mongo:7  (or an Atlas URI)
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Indexes are created at startup. A local `.env` is read too; real environment variables win.

## Notes and assumptions
- Instants are epoch milliseconds in the API and BSON dates in MongoDB (client is `tz_aware=True`). Calendar dates are IST strings.
- Analytics read the stored derived fields (`late_minutes`, `work_hours`, ...) and never recompute them.
- Rounding is half-up everywhere. Averages and rates are summed as exact decimals (`$toDecimal`) before rounding.
- `moving_avg_7d` averages the exact (unrounded) daily rates, then rounds the result.
- The department trend builds its day-by-day skeleton with `$range` and a window function (`$setWindowFields`) inside MongoDB, so empty days and weekends get a row.
- Concurrent PATCH: the update is conditional on the record being unchanged; the loser gets 409.
- Requires MongoDB 6.0+ (`$setWindowFields`, `$lookup` with `localField` + `pipeline`).

## Tests
```bash
pip install pytest httpx mongomock
python -m pytest tests -q                  # rules + CRUD against an in-memory fake
python tests/verify_analytics.py           # analytics, explain plans and concurrency on a REAL MongoDB
```
`tests/` is not needed to run the service.

## Status
See `REVIEW.md` (Part A) and `DECISIONS.md` (Part C).
