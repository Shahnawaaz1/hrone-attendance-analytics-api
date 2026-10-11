# DECISIONS.md

1. **Indexes.** I created unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` to enforce uniqueness. In `attendance_logs`, I added `(date: -1, emp_code: 1)` to eliminate collection scans and in-memory sorting on attendance listings, and `(date: 1, late_minutes: 1)` to accelerate date filtering and aggregation in the late leaderboard. In `employees`, I indexed `joined_on` and `(department, joined_on)` for headcount analytics.
2. **Punch-in race.** Simultaneous duplicate punch-ins both attempt an atomic `insert_one`. MongoDB's unique compound index ensures the first succeeds with 201, while the second raises `DuplicateKeyError`, caught and converted into HTTP 409 Conflict with zero duplicates created.
3. **Ties.** Ties use competition ranking via `$setWindowFields` (`$rank`) ordered by late minutes descending. Filtering by `rank <= limit` includes all tied candidates at the cutoff (e.g., if three share rank 10, all are returned).
4. **Headcount.** The aggregation starts from `employees` matching `joined_on <= end_date` and `$lookup`s attendance logs. This ensures zero-attendance staff are grouped and counted in department headcount.
5. **One thing you would change.** At 100x scale, I would maintain pre-aggregated daily/monthly rollups via change streams instead of scanning raw attendance logs on-the-fly.
