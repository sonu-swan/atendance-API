# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, lookups); `(department, emp_code)` for the filtered, sorted list; `(department, joined_on)` and `(joined_on)` for headcount. `attendance_logs`: unique `(emp_code, date)` (one record per day, the race guard, per-employee queries); `(date desc, emp_code)` for the default list and month scans; `(status, date, emp_code)` for status filters; `(emp_code, punch_in desc)` to find the record to punch out. Rejected: an index on `late_minutes`. The leaderboard already narrows by date, so it would not help.
2. **Punch-in race.** Both requests read the employee, then both call `insert_one`. The unique `(emp_code, date)` index lets one insert succeed (201). The other gets `DuplicateKeyError` and I return 409. No read-before-write.
3. **Ties.** `$rank` gives tied people the same rank, and I filter `rank <= limit` afterwards. So a tie at the cutoff returns every tied person, possibly more than `limit` rows.
4. **Headcount.** The pipeline starts from `employees` (joined on or before month end), then looks up logs. Someone with no logs gets an empty array, but their row still reaches `$group` and counts as 1.
5. **100x data.** Stop scanning raw logs for analytics: keep a small per-employee, per-day summary collection updated on every punch, and read that. Also use "after this date/emp_code" paging instead of `skip`.
