"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (real environment variables win over a local .env)

Layout of this file
  1. Config and database
  2. Time and number helpers (IST, epoch ms, half-up rounding)
  3. Business rules R1-R5 as small pure functions
  4. Request models (validation)
  5. Serialisation (Mongo document -> API JSON)
  6. Indexes (created at startup)
  7. Health, employees
  8. Attendance (punch-in, punch-out, list, regularize)
  9. Analytics (four aggregation pipelines)
 10. Explain endpoint
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Config and database
# --------------------------------------------------------------------------- #
load_dotenv()  # does not override variables that are already set in the environment

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
NOT_WORKED_STATUSES = ("ABSENT", "LEAVE")

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_LIMIT = Decimal("4.50")
MAX_SHIFT_SECONDS = 24 * 3600
MAX_TREND_DAYS = 92

_indexes_ready = False


# --------------------------------------------------------------------------- #
# 2. Time and number helpers
# --------------------------------------------------------------------------- #
def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC (defensive; the client already returns aware ones)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime -> integer epoch milliseconds. Integer maths, no float rounding."""
    if dt is None:
        return None
    return (as_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def parse_day(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def num(value: Any, places: int = 2) -> float:
    """Round a reported number half-up (R8). Goes through str() so 8.125 stays 8.125."""
    return float(half_up(Decimal(str(value)), places))


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    first = date(int(month[:4]), int(month[5:7]), 1)
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def count_weekdays(start: date, end: date) -> int:
    """Number of Monday-Friday days from start to end, both inclusive."""
    if end < start:
        return 0
    total = (end - start).days + 1
    full_weeks, extra = divmod(total, 7)
    count = full_weeks * 5
    for i in range(extra):
        if (start + timedelta(days=full_weeks * 7 + i)).weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# 3. Business rules R1-R5 (pure functions, no database access)
# --------------------------------------------------------------------------- #
def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST calendar date of the punch-in; early-morning punch-ins of an overnight
    shift belong to the shift that started the previous day."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < parse_hhmm(shift_end):
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, day: date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start, then whole minutes
    since shift_start (not since the end of the grace period)."""
    start = datetime.combine(day, parse_hhmm(shift_start), tzinfo=IST)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime(punch_out: datetime, day: date, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift_end, counted only when that is at least 30."""
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    end = datetime.combine(end_day, parse_hhmm(shift_end), tzinfo=IST)
    seconds = int((punch_out - end).total_seconds())
    minutes = seconds // 60 if seconds > 0 else 0
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: exact integer seconds / 3600, rounded half-up to 2 decimals."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float(half_up(Decimal(seconds) / Decimal(3600), 2))


def compute_half_day(work_hours: float) -> bool:
    """R5: based on the already-rounded work hours."""
    return Decimal(str(work_hours)) < HALF_DAY_LIMIT


def derived_fields(
    status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: date, emp: dict
) -> dict:
    """All four derived fields for a record in a given final state."""
    if status in NOT_WORKED_STATUSES or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, day, emp["shift_start"], emp["shift_end"])
        out["half_day"] = compute_half_day(out["work_hours"])
    return out


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


def check_date(value: str) -> str:
    if not DATE_RE.match(value):
        raise ValueError("must be a date in YYYY-MM-DD format")
    date.fromisoformat(value)  # rejects 2026-02-30
    return value


DateStr = Annotated[str, AfterValidator(check_date)]
# StrictInt: floats and strings are rejected; the range rejects values that look like seconds.
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Month = Annotated[str, Query(pattern=MONTH_PATTERN)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM_PATTERN)
    shift_end: str = Field(default="18:30", pattern=HHMM_PATTERN)
    joined_on: DateStr

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def no_explicit_nulls(cls, data):
        # Omitted means "unchanged"; an explicit null is not a valid value for these fields.
        if isinstance(data, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in data and data[key] is None:
                    raise ValueError(f"{key} must not be null")
        return data


def unprocessable(loc: list, msg: str) -> HTTPException:
    """422 with the same body shape FastAPI uses for its own validation errors."""
    return HTTPException(422, detail=[{"loc": loc, "msg": msg, "type": "value_error"}])


# --------------------------------------------------------------------------- #
# 5. Serialisation
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def _change_value(field: str, value: Any) -> Any:
    return to_ms(value) if field in ("punch_in", "punch_out") else value


def history_out(entry: dict) -> dict:
    return {
        "at": to_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": {
            field: {"from": _change_value(field, c.get("from")), "to": _change_value(field, c.get("to"))}
            for field, c in (entry.get("changes") or {}).items()
        },
    }


def record_out(doc: dict) -> dict:
    """Mongo document -> API record. Missing legacy fields become their defaults."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes") or 0),
        "overtime_minutes": int(doc.get("overtime_minutes") or 0),
        "half_day": bool(doc.get("half_day") or False),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# 6. Indexes (idempotent; run at startup)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    global _indexes_ready
    emp, logs = db.employees, db.attendance_logs
    # employees
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    emp.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    emp.create_index([("joined_on", ASCENDING)], name="joined_on")
    # attendance_logs
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_date_unique")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp")
    logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
    )
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the app up; /health reports 503 until MongoDB answers
        log.error("index creation failed at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_: Request, exc: PyMongoError):
    log.error("MongoDB error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 7. Health and employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index decides, not a prior find()
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {} if department is None else {"department": department}
    total = db.employees.count_documents(q) if q else db.employees.estimated_document_count()
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 8. Attendance
# --------------------------------------------------------------------------- #
def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(body.status, ts, None, day, emp),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index: exactly one winner
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record whose punch_in is at or before the punch-out: works across midnight.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    p_in = as_utc(rec["punch_in"])
    if ts <= p_in:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - p_in).total_seconds() > MAX_SHIFT_SECONDS:
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")
    fields = derived_fields(rec["status"], p_in, ts, parse_day(rec["date"]), emp)
    # Compare-and-set: only the request that still sees punch_out == null can win.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


def check_date_range(date_from: Optional[str], date_to: Optional[str]) -> None:
    if date_from and date_to and date_from > date_to:
        raise unprocessable(["query", "date_from"], "date_from must not be after date_to")


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    check_date_range(date_from, date_to)
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    cursor = db.attendance_logs.find(q, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")


@app.patch("/attendance/{emp_code}/{date}")
def regularize(body: RegularizeIn, emp_code: str, date: Annotated[DateStr, Path()]):
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    cur_in = as_utc(rec["punch_in"]) if rec.get("punch_in") else None
    cur_out = as_utc(rec["punch_out"]) if rec.get("punch_out") else None
    new_status = body.status or rec["status"]

    # ---- final state of the three editable fields
    if new_status in NOT_WORKED_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(["body"], "ABSENT and LEAVE clear the punch times; do not send them")
        new_in, new_out = None, None
    else:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else cur_in
        new_out = from_ms(body.punch_out) if body.punch_out is not None else cur_out
        if new_in is None:
            raise unprocessable(["body", "punch_in"], "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]).isoformat() != date:
            raise unprocessable(["body", "punch_in"], "punch_in must stay on the record's attendance date")
        if new_out is not None:
            gap = (new_out - new_in).total_seconds()
            if gap <= 0:
                raise unprocessable(["body", "punch_out"], "punch_out must be after punch_in")
            if gap > MAX_SHIFT_SECONDS:
                raise unprocessable(["body", "punch_out"], "punch_out is more than 24 hours after punch_in")

    if (new_status, new_in, new_out) == (rec["status"], cur_in, cur_out):
        raise unprocessable(["body"], "the request changes nothing")

    # ---- recompute derived fields from the final state (R2-R5)
    new_state = {"status": new_status, "punch_in": new_in, "punch_out": new_out}
    new_state.update(derived_fields(new_status, new_in, new_out, parse_day(date), emp))
    old_state = {
        "status": rec["status"],
        "punch_in": cur_in,
        "punch_out": cur_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": int(rec.get("late_minutes") or 0),
        "overtime_minutes": int(rec.get("overtime_minutes") or 0),
        "half_day": bool(rec.get("half_day") or False),
    }
    changes = {f: {"from": old_state[f], "to": new_state[f]} for f in CHANGE_FIELDS if old_state[f] != new_state[f]}
    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # ---- compare-and-set: apply only if nobody changed the record since we read it.
    # The history length acts as a version number; a legacy document may lack the field.
    n_history = len(rec.get("history") or [])
    if n_history == 0:
        history_unchanged: dict = {"$or": [{"history": {"$size": 0}}, {"history": {"$exists": False}}]}
    else:
        history_unchanged = {"history": {"$size": n_history}}
    guard = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        **history_unchanged,
    }
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new_state, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 9. Analytics (all aggregation pipelines)
# --------------------------------------------------------------------------- #
# Reusable aggregation expressions over one attendance document.
_DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}  # 1=Sun .. 7=Sat
_IS_WEEKDAY = {"$and": [{"$gte": [_DOW, 2]}, {"$lte": [_DOW, 6]}]}
_IS_PRESENT = {"$in": ["$status", PRESENT_STATUSES]}
_PRESENCE_WEIGHT = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
_LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
_IS_LATE = {"$gt": [_LATE_MIN, 0]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$gt": [{"$ifNull": ["$work_hours", None]}, None]}]}


def log_accumulators() -> dict:
    """$group accumulators shared by the employee and department summaries.
    They read the STORED derived values (late_minutes, overtime_minutes, half_day, work_hours)."""
    return {
        "present_days": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _PRESENCE_WEIGHT, 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [_IS_LATE, 1, 0]}},
        "total_late_minutes": {"$sum": _LATE_MIN},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "hours_sum": {"$sum": {"$cond": [_HAS_HOURS, "$work_hours", 0]}},
        "hours_count": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
    }


def monthly_pipeline(emp_code: str, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": None, **log_accumulators()}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = get_employee_or_404(emp_code)
    start, end = month_bounds(month)
    # R7: working days are Mon-Fri, counted from joined_on for mid-month joiners.
    working_days = count_weekdays(max(parse_day(start), parse_day(emp["joined_on"])), parse_day(end))
    agg = next(iter(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start, end))), {})
    present = Decimal(str(agg.get("present_days", 0)))
    pct = float(half_up(present / Decimal(working_days) * 100, 2)) if working_days > 0 else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": num(present),
        "leave_days": int(agg.get("leave_count", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


def department_summary_pipeline(start: str, end: str, department: Optional[str]) -> list:
    """Starts from `employees` so people with zero logs are counted (R9), then joins their logs."""
    emp_match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        emp_match["department"] = department

    def total(field):  # an employee with no logs has no joined sub-document: counts as 0
        return {"$sum": {"$ifNull": [f"$a.{field}", 0]}}

    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {"$group": {"_id": None, **log_accumulators()}},
                ],
                "as": "a",
            }
        },
        {"$set": {"a": {"$arrayElemAt": ["$a", 0]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": total("present_days"),
                "late_count": total("late_count"),
                "total_late_minutes": total("total_late_minutes"),
                "leave_count": total("leave_count"),
                "on_duty_count": total("on_duty_count"),
                "hours_sum": total("hours_sum"),
                "hours_count": total("hours_count"),
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    start, end = month_bounds(month)
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(start, end, department)):
        count = int(row["hours_count"])
        # mean over RECORDS: decimal sum / record count, then half-up (R8)
        avg = float(half_up(Decimal(str(round(row["hours_sum"], 6))) / Decimal(count), 2)) if count > 0 else None
        items.append(
            {
                "department": row["_id"],
                "headcount": int(row["headcount"]),
                "present_days": num(row["present_days"]),
                "avg_work_hours": avg,
                "late_count": int(row["late_count"]),
                "total_late_minutes": int(row["total_late_minutes"]),
                "leave_count": int(row["leave_count"]),
                "on_duty_count": int(row["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


def leaderboard_pipeline(start: str, end: str, limit: int, department: Optional[str]) -> list:
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": _LATE_MIN}, "late_count": {"$sum": {"$cond": [_IS_LATE, 1, 0]}}}},
        {"$match": {"total": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit is applied AFTER ranking
        {"$sort": {"total": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": "$total",
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    start, end = month_bounds(month)
    rows = db.attendance_logs.aggregate(leaderboard_pipeline(start, end, limit, department))
    items = [
        {
            "rank": int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]),
            "late_count": int(r["late_count"]),
        }
        for r in rows
    ]
    return {"month": month, "items": items}


def trend_pipeline(department: str, start: str, end: str) -> list:
    """One row per calendar day. Rounding uses integer arithmetic because MongoDB's $round
    is banker's rounding and the contract asks for half-up."""
    codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})]
    if not codes:
        raise HTTPException(404, "unknown department")
    first = datetime.combine(parse_day(start), time(0), tzinfo=UTC)
    after_last = datetime.combine(parse_day(end), time(0), tzinfo=UTC) + timedelta(days=1)
    day_str = {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}}
    dow = {"$dayOfWeek": "$d"}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": start, "$lte": end}}},
        {
            "$project": {
                "_id": 0,
                "d": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "p": {"$cond": [_IS_PRESENT, _PRESENCE_WEIGHT, 0]},
                "l": {"$cond": [_IS_LATE, 1, 0]},
            }
        },
        # A zero-valued seed row for the first day: guarantees $densify always has input,
        # even when the department has no records at all in the range.
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {"$project": {"_id": 0, "d": {"$literal": first}, "p": {"$literal": 0}, "l": {"$literal": 0}}},
                ],
            }
        },
        {"$group": {"_id": "$d", "present": {"$sum": "$p"}, "late": {"$sum": "$l"}}},
        {"$project": {"_id": 0, "d": "$_id", "present": 1, "late": 1}},
        {"$sort": {"d": 1}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [first, after_last]}}},
        {"$set": {"present": {"$ifNull": ["$present", 0]}, "late": {"$ifNull": ["$late", 0]}}},
        {
            "$lookup": {  # R9: headcount that day = department employees with joined_on <= day
                "from": "employees",
                "let": {"day": day_str},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {
            "$set": {
                "date": day_str,
                "is_working_day": {"$and": [{"$gte": [dow, 2]}, {"$lte": [dow, 6]}]},
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            }
        },
        {"$set": {"is_rated": {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}}},
        {
            "$set": {
                # rate * 10000 rounded half-up = floor((2 * present * 10000 + headcount) / (2 * headcount))
                "rate_k": {
                    "$cond": [
                        "$is_rated",
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": ["$present", 20000]}, "$headcount"]},
                                    {"$multiply": ["$headcount", 2]},
                                ]
                            }
                        },
                        0,
                    ]
                },
                "rated": {"$cond": ["$is_rated", 1, 0]},
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {
                    "win_k": {"$sum": "$rate_k", "window": {"documents": [-6, 0]}},
                    "win_n": {"$sum": "$rated", "window": {"documents": [-6, 0]}},
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": "$present",
                "late_count": "$late",
                "attendance_rate": {"$cond": ["$is_rated", {"$divide": ["$rate_k", 10000]}, None]},
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": ["$win_n", 0]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {"$add": [{"$multiply": ["$win_k", 2]}, "$win_n"]},
                                            {"$multiply": ["$win_n", 2]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
    ]


def check_trend_range(start: str, end: str) -> None:
    s, e = parse_day(start), parse_day(end)
    if e < s:
        raise unprocessable(["query", "to"], "to must not be before from")
    if (e - s).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(["query", "to"], f"range must not exceed {MAX_TREND_DAYS} days")


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[DateStr, Query(alias="from")], to: DateStr):
    check_trend_range(from_, to)
    rows = db.attendance_logs.aggregate(trend_pipeline(department, from_, to))
    items = [
        {
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": num(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": r.get("attendance_rate"),
            "moving_avg_7d": r.get("moving_avg_7d"),
        }
        for r in rows
    ]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 10. Explain endpoint
# --------------------------------------------------------------------------- #
ExplainTarget = Literal[
    "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
]


def _require(value: Any, name: str):
    if value is None:
        raise unprocessable(["query", name], "field required")
    return value


def _valid_month(value: Optional[str]) -> str:
    _require(value, "month")
    if not re.match(MONTH_PATTERN, value):
        raise unprocessable(["query", "month"], "month must be YYYY-MM")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,
    to: Optional[DateStr] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    """Explain the SAME query/pipeline the real endpoint builds (the builders are shared)."""
    ensure_indexes()  
    if endpoint == "attendance_list":
        check_date_range(date_from, date_to)
        collection = "attendance_logs"
        command: dict = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            _require(emp_code, "emp_code")
            start, end = month_bounds(_valid_month(month))
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, start, end)
        elif endpoint == "department_summary":
            start, end = month_bounds(_valid_month(month))
            collection, pipeline = "employees", department_summary_pipeline(start, end, department)
        elif endpoint == "late_leaderboard":
            start, end = month_bounds(_valid_month(month))
            collection, pipeline = "attendance_logs", leaderboard_pipeline(start, end, limit, department)
        else:  # department_trend
            _require(department, "department")
            _require(from_, "from")
            _require(to, "to")
            check_trend_range(from_, to)
            collection, pipeline = "attendance_logs", trend_pipeline(department, from_, to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS)),
    }
