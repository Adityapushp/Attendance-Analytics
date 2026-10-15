# DECISIONS.md

1. **Indexes.** On `attendance_logs`: unique `(emp_code, date)` makes duplicate punch-ins impossible and serves per-employee ranges; `(date desc, emp_code)` matches the list sort order and serves date ranges plus the month-wide leaderboard. On `employees`: unique `emp_code`; `(department, emp_code)` for filtered lists and the trend; `(joined_on)` for headcount. Rejected: an index on `status` (low selectivity) and `(emp_code, punch_in)` for punch-out, because one employee has few records and the existing index is enough.
2. **Punch-in race.** Both requests compute the date and call `insert_one`. The unique index lets the database accept one; the other gets `DuplicateKeyError`, which I turn into 409. No find-then-insert.
3. **Ties.** `$setWindowFields` with `$rank` gives 1, 2, 2, 4. I keep `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even if that exceeds `limit`. Rows are ordered by minutes, then `emp_code`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and looks up each person's logs, so someone with no logs still counts once.
5. **100x data.** Store department on each log and keep daily rollups updated on write, so analytics read small summary documents instead of scanning raw logs.
