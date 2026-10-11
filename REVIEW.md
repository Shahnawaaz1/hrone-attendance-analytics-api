# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `POST /employees` | Missing `shift_start != shift_end` check | Input `shift_start="09:30"`, `shift_end="09:30"` returns 201 instead of 422 | Added check in `create_employee` returning 422 |
| 2 | `POST /employees`, `POST /attendance/punch-in` | Concurrency race condition: `find_one` then `insert_one` | 2 concurrent identical requests both pass `find_one` and create duplicates | Switched to `db.collection.insert_one()` catching `DuplicateKeyError`, configured unique index at startup |
| 3 | `POST /attendance/punch-in`, Helpers | `punched_at` not truncated to whole seconds | Truncated seconds show up as milliseconds in `created_at` or `punch_in` | Added `truncate_to_seconds(dt)` before using timestamps |
| 4 | `POST /attendance/punch-in`, Helpers | Ignored timezone, used naive local `datetime.now()` | `datetime.now()` treated as UTC by PyMongo, shifting times by 5.5 hrs | Replaced with `datetime.now(timezone.utc)` and explicit `IST` conversions |
| 5 | `POST /attendance/punch-in` | Overnight shift date assignment bug | Punch-in at 05:00 for a 22:00-06:00 shift incorrectly sets `date` to today, not yesterday | Added `get_attendance_date` with previous-day logic for times < `shift_end` |
| 6 | `compute_late_minutes` | `start` date logic breaks when `punch_in` is on a different calendar day | Calculation produces wrong late minutes or crashes on overnight shifts | Fixed to construct `start` using the attendance `date` and `shift_start` time |
| 7 | `GET /employees`, `GET /attendance` | `total` incorrectly counts entire collection | Passing a query param (`department="HR"`) gives wrong `total` count in pagination | Changed `count_documents({})` to `count_documents(q)` |
| 8 | `GET /attendance` | In-memory sort and pagination | Huge memory usage and slow response for 100k records | Delegated sort and pagination to MongoDB `.sort().skip().limit()` |
| 9 | `POST /attendance/punch-in`, `GET /attendance` | Response serialization violates OpenAPI schema | `id` field present, dates returned as ISO strings instead of epoch ms | Wrote formatter to strip `_id`, remove `id`, convert datetimes to epoch ms |
| 10| `GET /attendance` | Missing `date_from > date_to` validation | `date_from="2026-02-01"`, `date_to="2026-01-01"` returns 200 instead of 422 | Added manual `date_from > date_to` validation check |

Also note anything you looked at and decided was **not** a defect, and why.
- `GET /health` responding with just `{"status": "ok"}`: The ping checks connectivity to MongoDB properly now, and the response matches the OpenAPI spec schema perfectly.
