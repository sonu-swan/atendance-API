# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # or your Atlas URI; a local .env also works
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent). `GET /health` returns 200 once MongoDB answers.
Needs MongoDB 6.0+ (`$densify`, `$setWindowFields`, `$lookup` with `localField` + `pipeline`).

## Choices where the contract leaves room
- **Moving average** is taken over the *reported* (4-decimal rounded) `attendance_rate` values.
- **Trend range limit:** 92 calendar days *inclusive* is allowed; 93 is 422.
- **Punch-out** looks for the latest record with `punch_in <= punched_at`. An earlier time finds nothing (404). An equal time is 422.
- **Concurrent PATCH:** the loser gets 409 (compare-and-set on status, punch times and history length). There is no automatic retry.
- **"Request changes nothing"** (422) is judged on `status`, `punch_in`, `punch_out`.
- Rounding is half-up everywhere. Where MongoDB's `$round` (banker's) would be wrong, the pipelines use integer arithmetic.

## Missing / known gaps
None intended. See REVIEW.md and DECISIONS.md.
