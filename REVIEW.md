# REVIEW.md

Review of the starter `app/main.py` (line numbers refer to the original starter).

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` (L33-37) | Works on the raw UTC datetime and puts the shift time on the **UTC** date, not IST. | Punch-in 10:15:59 IST (04:45:59 UTC), shift 09:30 -> compared with 09:30 UTC -> negative -> 0. Anyone in IST is "never late". | Convert to IST first. The shift start is built on the attendance date in IST. |
| 2 | `compute_late_minutes` (L36-37) | Floors to minutes first, then tests `> 10`. R2 says strictly more than 10:00. | 09:40:01 -> 10 -> `10 > 10` is false -> 0. Should be 10. | Compare in seconds (`> 600`), then floor to minutes. |
| 3 | `compute_late_minutes` | Ignores overnight shifts (R1) and does not truncate to whole seconds. | Shift 22:00-06:00, punch-in 00:30 next day: start is built on the wrong day. 09:40:00.900 should count as 09:40:00. | Attendance date comes from `attendance_date()`. Instants are truncated to seconds when read from the request. |
| 4 | `compute_work_hours` (L40-41) | Python `round()` is float rounding / banker's, not half-up (R4). | 3618 seconds = 1.005 h -> `round` gives 1.0, should be 1.01. | Exact integer seconds with `Decimal`, `ROUND_HALF_UP`. |
| 5 | `compute_overtime` (L44-47) | No 30-minute minimum (R3). | Sample 1: out 18:35, shift end 18:30 -> returns 5, should be 0. | Return the minutes only if >= 30. |
| 6 | `compute_overtime` | Shift end is always on the record's date, so overnight shifts are wrong; a naive `punch_out` crashes against the aware `end`. | Shift 22:00-06:00, out 06:40 next day -> "end" is 06:00 on the *start* day -> huge number or 0, not 40. | End is on the next day when `shift_end <= shift_start`. All datetimes are aware (UTC). |
| 7 | `MongoClient` (L22) | Naive datetimes come back from PyMongo, mixed with aware ones elsewhere. | `TypeError: can't subtract offset-naive and offset-aware datetimes` in punch-out. | `tz_aware=True`; every instant is UTC-aware; `to_ms`/`from_ms` at the API edge. |
| 8 | `EmployeeIn` (L53-60) | No validation at all. | `emp_code` "x", email "abc", shift "25:99", `shift_start == shift_end`, `joined_on` "2026-02-30" are all accepted. Later `int("xx")` raises -> 500. | Patterns, lengths, real-date check, `shift_start != shift_end`. |
| 9 | `PunchInIn` (L63-66) | `punched_at` is a lax int; `status` is a free string. | Seconds (1783312500), the float 1.5e12 and the string "1783312500000" are accepted; `ABSENT` / `"banana"` can be punched in. | Strict int in the ms range; status limited to PRESENT / WFH / ON_DUTY. |
| 10 | `/health` (L72-74) | Always returns 200. | Stop MongoDB: health still says ok. | `ping`; 503 on failure. |
| 11 | `create_employee` (L77-85) | Check-then-insert race and no unique index. | Two parallel creates of the same `emp_code` both get 201. | Unique index on `emp_code`; `DuplicateKeyError` -> 409. |
| 12 | `create_employee` | `created_at = datetime.now()` is naive local time and is returned as a datetime. | Server in another time zone gives wrong stored values; response is not epoch ms. | UTC, stored as BSON date, returned as epoch ms. |
| 13 | `list_employees` (L89-96) | `skip = page * page_size` skips a page. `total` counts all employees, not the filtered set. No sort. No limits on `page` / `page_size`. | `page=1` returns the second page. `?department=HR` shows the wrong total. `page=0` -> negative skip -> 500. `page_size=0` means "no limit". | `(page-1) * page_size`, `count_documents(filter)`, sort by `emp_code`, `page >= 1`, `1 <= page_size <= 100`. |
| 14 | `punch_in` (L101) | Unknown employee: `emp` is None, then `emp["shift_start"]` -> `TypeError`. | POST with an unknown `emp_code` -> 500. | 404. |
| 15 | `punch_in` (L102-103) | `datetime.fromtimestamp(ms/1000)` is naive **server-local** time; the date is taken from it. R1 needs the IST date; float division loses the whole-second truncation. | Server on UTC: a punch-in at 00:10 IST (18:40 UTC the day before) gets yesterday's date. | `attendance_date()`: IST date, overnight rule, truncated seconds. |
| 16 | `punch_in` (L104-118) | Check-then-insert race. | Parallel identical punch-ins: several 201, duplicate records for one day. | Unique index on (`emp_code`, `date`); insert; `DuplicateKeyError` -> 409. |
| 17 | `punch_in` response (L119-121) | Leaks `_id` as `id`; `punch_in` is a datetime, not epoch ms. | Response has an `id` field the contract forbids. | Shared `record_out()` serialiser. |
| 18 | `list_attendance` (L144-150) | Loads **every** matching document, sorts and slices in Python. No index. | 100k records: slow and memory heavy. | `find().sort().skip().limit()` backed by indexes; `count_documents`. |
| 19 | `list_attendance` | Sort is by `date` only (no `emp_code` tie-break), so page boundaries are unstable. No validation of dates, status, page, page_size. | `date_from=garbage` silently returns wrong rows; `date_from > date_to` returns an empty list instead of 422. | Sort (`date` desc, `emp_code` asc); date/status/page validation; 422. |
| 20 | `list_attendance` | Legacy documents without `half_day` / `history` are returned as they are; `_id` leaks as `id`. | Response misses required fields for old records. | `record_out()` fills defaults (0, false, []). |
| 21 | Whole file | No indexes at all; every query is a collection scan. | `explain()` shows COLLSCAN. | `ensure_indexes()` at startup, idempotent. |
| 22 | Whole file | MongoDB outages surface as 500. | Stop MongoDB, call any endpoint. | Global handler: `PyMongoError` -> 503. |

## Looked at and decided it was fine
- `load_dotenv()` does not override real environment variables (default `override=False`), so the "real env wins" rule already holds.
- Sync `def` endpoints with sync PyMongo: FastAPI runs them in a thread pool, so they do not block the event loop. `MongoClient` is thread-safe. I kept this and did not move to Motor.
- Creating the client at import time: connection is lazy, so the app still starts when MongoDB is down.
- `HTTPException` bodies already match the `{"detail": ...}` contract.
