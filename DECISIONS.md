DECISIONS.md

1. Indexes. On attendance_logs: a unique (emp_code, date) index stops duplicate records and speeds up one employee's date range, and (date desc, emp_code) matches the list sort and month-wide queries. On employees: unique emp_code, (department, emp_code) for department filters, and joined_on for headcount. I skipped an index on status because it has too few values to filter well.

2. Punch-in race. Both requests work out the date and call insert_one. The unique index lets only one succeed. The other raises DuplicateKeyError and I return 409. I never look first and insert later, because that leaves a gap.

3. Ties. $rank gives ties the same rank and skips the next (1, 2, 2, 4). I apply limit after ranking, so everyone tied at the cutoff is returned, even if that is more than limit rows. Ties are ordered by emp_code.

4. Headcount. The summary starts from employees, not the logs. Everyone who joined by month end is counted, then their logs are looked up, so people with zero logs still count.

5. 100x data. I would store the department on each log and keep small daily totals updated on write, so analytics read summaries instead of scanning raw logs.