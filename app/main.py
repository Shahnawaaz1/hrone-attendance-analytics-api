import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone, date as datetime_date
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
import pymongo
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP
import bson.json_util as json_util
import json

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes idempotently at startup
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", pymongo.ASCENDING), ("date", pymongo.ASCENDING)], unique=True)
    db.employees.create_index("department")
    db.attendance_logs.create_index("date")
    yield

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def truncate_to_seconds(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)

def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def make_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

def get_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    d = punch_in_ist.date()
    if shift_end <= shift_start:
        h, m = map(int, shift_end.split(':'))
        if punch_in_ist.time() < datetime(2000, 1, 1, h, m).time():
            d -= timedelta(days=1)
    return d.isoformat()

def compute_late_minutes(punch_in_ist: datetime, shift_start: str, attendance_date_str: str) -> int:
    h, m = map(int, shift_start.split(":"))
    ad = datetime_date.fromisoformat(attendance_date_str)
    start_dt = datetime(ad.year, ad.month, ad.day, h, m, tzinfo=IST)
    minutes = int((punch_in_ist - start_dt).total_seconds() / 60)
    return minutes if minutes > 10 else 0

def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((punch_out - punch_in).total_seconds()))
    hours = seconds / Decimal('3600')
    return float(hours.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))

def compute_overtime(punch_out_ist: datetime, shift_end: str, attendance_date_str: str, shift_start: str) -> int:
    h, m = map(int, shift_end.split(":"))
    ad = datetime_date.fromisoformat(attendance_date_str)
    end_dt = datetime(ad.year, ad.month, ad.day, h, m, tzinfo=IST)
    if shift_end <= shift_start:
        end_dt += timedelta(days=1)
    minutes = int((punch_out_ist - end_dt).total_seconds() / 60)
    return minutes if minutes >= 30 else 0

def compute_half_day(work_hours: float) -> bool:
    return work_hours < 4.50

def format_employee_doc(doc: dict) -> dict:
    doc.pop("_id", None)
    doc["created_at"] = to_epoch_ms(doc.get("created_at"))
    return doc

def format_attendance_doc(doc: dict) -> dict:
    doc.pop("_id", None)
    doc.pop("id", None)
    doc["punch_in"] = to_epoch_ms(doc.get("punch_in"))
    doc["punch_out"] = to_epoch_ms(doc.get("punch_out"))
    
    for entry in doc.get("history", []):
        entry["at"] = to_epoch_ms(entry.get("at"))
        changes = entry.get("changes", {})
        for field in ["punch_in", "punch_out"]:
            if field in changes:
                changes[field]["from"] = to_epoch_ms(changes[field].get("from"))
                changes[field]["to"] = to_epoch_ms(changes[field].get("to"))
                
    return doc

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

class RegularizeIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None

# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command('ping')
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Service Unavailable")

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail=[{"loc": ["body", "shift_end"], "msg": "shift_start and shift_end must differ", "type": "value_error"}])
        
    doc = body.model_dump()
    doc["created_at"] = truncate_to_seconds(datetime.now(timezone.utc))
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")
    
    return format_employee_doc(doc)

@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
        
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", pymongo.ASCENDING).skip(skip).limit(page_size)
    items = [format_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in ("PRESENT", "WFH", "ON_DUTY"):
        raise HTTPException(422, detail=[{"loc": ["body", "status"], "msg": "Invalid status", "type": "value_error"}])

    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
    else:
        ts = datetime.now(timezone.utc)
    
    ts = truncate_to_seconds(ts)
    ts_ist = ts.astimezone(IST)
    
    d = get_attendance_date(ts_ist, emp["shift_start"], emp["shift_end"])
    
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts_ist, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="already punched in for this date")
        
    return format_attendance_doc(doc)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail=[{"loc": ["query", "date_from"], "msg": "date_from > date_to", "type": "value_error"}])

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
        
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    
    cursor = db.attendance_logs.find(q).sort([("date", pymongo.DESCENDING), ("emp_code", pymongo.ASCENDING)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

# --------------------------------------------------------------------------- #
# Analytics and Admin
# --------------------------------------------------------------------------- #
import calendar

# --------------------------------------------------------------------------- #
# Analytics Helpers
# --------------------------------------------------------------------------- #
def get_month_boundaries(month_str: str):
    y, m = map(int, month_str.split("-"))
    start_date = datetime_date(y, m, 1)
    last_day = calendar.monthrange(y, m)[1]
    end_date = datetime_date(y, m, last_day)
    return start_date.isoformat(), end_date.isoformat()

def get_working_days_in_month(month_str: str, joined_on_str: str) -> int:
    y, m = map(int, month_str.split("-"))
    last_day = calendar.monthrange(y, m)[1]
    
    joined_date = datetime_date.fromisoformat(joined_on_str)
    month_start = datetime_date(y, m, 1)
    
    count = 0
    for day in range(1, last_day + 1):
        d = datetime_date(y, m, day)
        if d >= joined_date and d.weekday() < 5: # Mon=0, Fri=4
            count += 1
    return count

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
        
    start_date, end_date = get_month_boundaries(month)
    working_days = get_working_days_in_month(month, emp["joined_on"])
    
    pipeline = [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": start_date, "$lte": end_date}
        }},
        {"$addFields": {
            "date_obj": {"$dateFromString": {"dateString": "$date"}},
            "is_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]} # Mongo 1=Sun, 2=Mon...6=Fri
        }},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$is_weekday"]},
                        {"$cond": ["$half_day", 0.5, 1.0]},
                        0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$late_minutes", 0]}
            },
            "total_overtime_minutes": {
                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
            }
        }}
    ]
    
    res = list(db.attendance_logs.aggregate(pipeline))
    if not res:
        agg = {
            "present_days": 0.0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0
        }
    else:
        agg = res[0]
        
    attendance_pct = None
    if working_days > 0:
        pct = (agg["present_days"] / working_days) * 100
        attendance_pct = float(Decimal(str(pct)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
        
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(agg["present_days"]),
        "leave_days": agg["leave_days"],
        "late_count": agg["late_count"],
        "total_late_minutes": agg["total_late_minutes"],
        "total_overtime_minutes": agg["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    start_date, end_date = get_month_boundaries(month)
    
    emp_match = {"joined_on": {"$lte": end_date}}
    if department:
        emp_match["department"] = department
        
    pipeline = [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp_code": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$emp_code", "$$emp_code"]},
                    "date": {"$gte": start_date, "$lte": end_date}
                }},
                {"$addFields": {
                    "is_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"dept": "$department", "emp": "$emp_code"},
            "emp_present": {
                "$sum": {
                    "$cond": [
                        {"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$logs.is_weekday"]},
                        {"$cond": ["$logs.half_day", 0.5, 1.0]},
                        0
                    ]
                }
            },
            "emp_late_count": {
                "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
            },
            "emp_late_mins": {
                "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
            },
            "emp_leave": {
                "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
            },
            "emp_duty": {
                "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
            },
            "emp_work_hours": {
                "$push": {"$cond": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$logs.work_hours", "$$REMOVE"]}
            }
        }},
        {"$group": {
            "_id": "$_id.dept",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$emp_present"},
            "late_count": {"$sum": "$emp_late_count"},
            "total_late_minutes": {"$sum": "$emp_late_mins"},
            "leave_count": {"$sum": "$emp_leave"},
            "on_duty_count": {"$sum": "$emp_duty"},
            "all_work_hours": {"$push": "$emp_work_hours"}
        }},
        {"$sort": {"_id": 1}}
    ]
    
    res = list(db.employees.aggregate(pipeline))
    items = []
    for r in res:
        # compute average work hours
        wh_list = []
        for emp_hrs in r.get("all_work_hours", []):
            wh_list.extend([h for h in emp_hrs if h is not None])
            
        avg_wh = None
        if wh_list:
            avg = sum(wh_list) / len(wh_list)
            avg_wh = float(Decimal(str(avg)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
            
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"]
        })
        
    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    start_date, end_date = get_month_boundaries(month)
    
    emp_match = {}
    if department:
        emp_match["department"] = department
        
    pipeline = [
        {"$match": {
            "date": {"$gte": start_date, "$lte": end_date},
            "late_minutes": {"$gt": 0}
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
        
    pipeline.extend([
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}}
    ])
    
    res = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in res:
        items.append({
            "rank": r["rank"],
            "emp_code": r["_id"],
            "name": r["emp"]["name"],
            "department": r["emp"]["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"]
        })
        
    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str, to_date: str):
    from fastapi import Query
    if from_date > to_date:
        raise HTTPException(422, detail="from_date > to_date")
    f_dt = datetime_date.fromisoformat(from_date)
    t_dt = datetime_date.fromisoformat(to_date)
    if (t_dt - f_dt).days > 92:
        raise HTTPException(422, detail="range exceeds 92 days")
        
    # verify department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, detail="Department not found")

    # This pipeline is highly complex. We will use densify.
    pipeline = [
        # First, match all employees in the department to count daily headcount
        # But we need dates. $densify can create a sequence of dates.
        # Alternatively, generate the date sequence in Python, then do queries.
        # Requirement: "use a gap-filling stage such as $densify; do not loop in Python"
        # Since we must return exactly one row per calendar day, we can start from attendance_logs,
        # match the department, densify, then lookup.
    ]
    
    # We will build a pipeline that uses $documents to seed the date range,
    # then $lookup to employees to get headcount, and $lookup to attendance to get present/late.
    # $documents is available in MongoDB 5.1+, the assignment assumes MongoDB 6.0+.
    # Or we can just query the data and aggregate.
    pipeline = [
        {"$documents": [{"dummy": 1}]},
        {"$densify": {
            "field": "date_obj",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [datetime.fromisoformat(from_date), datetime.fromisoformat(to_date) + timedelta(days=1)]
            }
        }},
        {"$match": {"date_obj": {"$type": "date"}}},
        {"$project": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$date_obj"}, [2, 3, 4, 5, 6]]}
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {
                    "department": department,
                    "$expr": {"$lte": ["$joined_on", "$$d"]}
                }},
                {"$count": "headcount"}
            ],
            "as": "hc"
        }},
        {"$addFields": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.headcount", 0]}, 0]}
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {
                    "$expr": {"$eq": ["$date", "$$d"]}
                }},
                {"$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp"
                }},
                {"$match": {"emp.department": department}},
                {"$group": {
                    "_id": None,
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$cond": ["$half_day", 0.5, 1.0]},
                                0
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                    }
                }}
            ],
            "as": "logs"
        }},
        {"$addFields": {
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$logs.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$logs.late_count", 0]}, 0]}
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]}
                }
            }
        }},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1
        }},
        {"$sort": {"date": 1}}
    ]
    
    command_res = db.command({
        "aggregate": 1,
        "pipeline": pipeline,
        "cursor": {}
    })
    res = command_res.get("cursor", {}).get("firstBatch", [])
    items = []
    for r in res:
        rate = r["attendance_rate"]
        if rate is not None:
            rate = float(Decimal(str(rate)).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))
            
        m_avg = r["moving_avg_7d"]
        if m_avg is not None:
            m_avg = float(Decimal(str(m_avg)).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP))
            
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": float(r["present_count"]),
            "late_count": r["late_count"],
            "attendance_rate": rate,
            "moving_avg_7d": m_avg
        })
        
    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: Optional[int] = Query(1, ge=1),
    page_size: Optional[int] = Query(20, ge=1, le=100)
):
    if endpoint not in ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
        raise HTTPException(422, detail="Invalid endpoint")
        
    collection = ""
    explain = {}
    
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        explain = db.command({
            "explain": {
                "find": collection,
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size
            },
            "verbosity": "executionStats"
        })
        
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="Missing emp_code or month")
        collection = "attendance_logs"
        start_date, end_date = get_month_boundaries(month)
        pipeline = [
            {"$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date}
            }},
            {"$addFields": {
                "date_obj": {"$dateFromString": {"dateString": "$date"}},
                "is_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
            }},
            {"$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$is_weekday"]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }}
        ]
        explain = db.command({
            "aggregate": collection,
            "pipeline": pipeline,
            "explain": True,
            "cursor": {}
        })
        
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="Missing month")
        collection = "employees"
        start_date, end_date = get_month_boundaries(month)
        emp_match = {"joined_on": {"$lte": end_date}}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"emp_code": "$emp_code"},
                "pipeline": [
                    {"$match": {
                        "$expr": {"$eq": ["$emp_code", "$$emp_code"]},
                        "date": {"$gte": start_date, "$lte": end_date}
                    }},
                    {"$addFields": {
                        "is_weekday": {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]}
                    }}
                ],
                "as": "logs"
            }},
            {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": {"dept": "$department", "emp": "$emp_code"},
                "emp_present": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$logs.is_weekday"]},
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0
                        ]
                    }
                },
                "emp_late_count": {
                    "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
                },
                "emp_late_mins": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "emp_leave": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
                },
                "emp_duty": {
                    "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
                },
                "emp_work_hours": {
                    "$push": {"$cond": [{"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$logs.work_hours", "$$REMOVE"]}
                }
            }},
            {"$group": {
                "_id": "$_id.dept",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$emp_present"},
                "late_count": {"$sum": "$emp_late_count"},
                "total_late_minutes": {"$sum": "$emp_late_mins"},
                "leave_count": {"$sum": "$emp_leave"},
                "on_duty_count": {"$sum": "$emp_duty"},
                "all_work_hours": {"$push": "$emp_work_hours"}
            }},
            {"$sort": {"_id": 1}}
        ]
        explain = db.command({
            "aggregate": collection,
            "pipeline": pipeline,
            "explain": True,
            "cursor": {}
        })
        
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="Missing month")
        collection = "attendance_logs"
        start_date, end_date = get_month_boundaries(month)
        pipeline = [
            {"$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0}
            }},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }},
            {"$unwind": "$emp"},
        ]
        if department:
            pipeline.append({"$match": {"emp.department": department}})
        pipeline.extend([
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}}
        ])
        explain = db.command({
            "aggregate": collection,
            "pipeline": pipeline,
            "explain": True,
            "cursor": {}
        })
        
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, detail="Missing department, from, or to")
        collection = "1"
        pipeline = [
            {"$documents": [{"dummy": 1}]},
            {"$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [datetime.fromisoformat(from_date), datetime.fromisoformat(to_date) + timedelta(days=1)]
                }
            }},
            {"$match": {"date_obj": {"$type": "date"}}},
            {"$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                "is_working_day": {"$in": [{"$dayOfWeek": "$date_obj"}, [2, 3, 4, 5, 6]]}
            }},
            {"$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {
                        "department": department,
                        "$expr": {"$lte": ["$joined_on", "$$d"]}
                    }},
                    {"$count": "headcount"}
                ],
                "as": "hc"
            }},
            {"$addFields": {
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.headcount", 0]}, 0]}
            }},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {
                        "$expr": {"$eq": ["$date", "$$d"]}
                    }},
                    {"$lookup": {
                        "from": "employees",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "emp"
                    }},
                    {"$match": {"emp.department": department}},
                    {"$group": {
                        "_id": None,
                        "present_count": {
                            "$sum": {
                                "$cond": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$cond": ["$half_day", 0.5, 1.0]},
                                    0
                                ]
                            }
                        },
                        "late_count": {
                            "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                        }
                    }}
                ],
                "as": "logs"
            }},
            {"$addFields": {
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$logs.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$logs.late_count", 0]}, 0]}
            }},
            {"$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }},
            {"$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]}
                    }
                }
            }},
            {"$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1
            }},
            {"$sort": {"date": 1}}
        ]
        explain = db.command({
            "aggregate": 1,
            "pipeline": pipeline,
            "explain": True,
            "cursor": {}
        })

    # Safely serialize explain output
    explain = json.loads(json_util.dumps(explain))

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain
    }

@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    if body.punched_at is not None:
        ts = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
    else:
        ts = datetime.now(timezone.utc)
    ts = truncate_to_seconds(ts)
    
    q = {
        "emp_code": body.emp_code,
        "punch_in": {"$lte": ts}
    }
    rec = db.attendance_logs.find_one(q, sort=[("date", pymongo.DESCENDING)])
    if not rec:
        raise HTTPException(404, detail="No open punch-in found")
        
    rec_punch_in = make_aware(rec.get("punch_in"))
    rec_punch_out = make_aware(rec.get("punch_out"))
        
    if rec_punch_out is not None:
        raise HTTPException(409, detail="Already punched out")
        
    if (ts - rec_punch_in).total_seconds() > 24 * 3600 or ts <= rec_punch_in:
        raise HTTPException(422, detail="Invalid punch_out time")
        
    ts_ist = ts.astimezone(IST)
    wh = compute_work_hours(rec_punch_in, ts)
    om = compute_overtime(ts_ist, emp["shift_end"], rec["date"], emp["shift_start"])
    hd = compute_half_day(wh)
    
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {
            "punch_out": ts,
            "work_hours": wh,
            "overtime_minutes": om,
            "half_day": hd
        }},
        return_document=pymongo.ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(409, detail="Already punched out concurrently")
        
    return format_attendance_doc(updated)

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, detail="Employee not found")
        
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, detail="Record not found")
        
    changes = {}
    updates = {}
    
    new_status = body.status if body.status is not None else rec["status"]
    
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, detail=[{"loc": ["body"], "msg": "Cannot supply punch times with ABSENT/LEAVE", "type": "value_error"}])
        new_in = None
        new_out = None
    else:
        new_in = None
        if body.punch_in is not None:
            new_in = truncate_to_seconds(datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc))
        else:
            new_in = make_aware(rec.get("punch_in"))
            
        if not new_in:
            raise HTTPException(422, detail=[{"loc": ["body"], "msg": "Presence status requires a punch_in", "type": "value_error"}])
            
        new_out = None
        if body.punch_out is not None:
            new_out = truncate_to_seconds(datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc))
        else:
            new_out = make_aware(rec.get("punch_out"))

        in_ist = new_in.astimezone(IST)
        expected_date = get_attendance_date(in_ist, emp["shift_start"], emp["shift_end"])
        if expected_date != date:
            raise HTTPException(422, detail=[{"loc": ["body", "punch_in"], "msg": "punch_in must stay on the record's attendance date", "type": "value_error"}])
            
        if new_out is not None:
            if new_out <= new_in or (new_out - new_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, detail=[{"loc": ["body", "punch_out"], "msg": "punch_out must be after punch_in and within 24 hours", "type": "value_error"}])

    new_late = 0
    new_wh = None
    new_om = 0
    new_hd = False
    
    if new_status not in ("ABSENT", "LEAVE"):
        in_ist = new_in.astimezone(IST)
        new_late = compute_late_minutes(in_ist, emp["shift_start"], date)
        if new_out is not None:
            out_ist = new_out.astimezone(IST)
            new_wh = compute_work_hours(new_in, new_out)
            new_om = compute_overtime(out_ist, emp["shift_end"], date, emp["shift_start"])
            new_hd = compute_half_day(new_wh)

    field_map = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_om,
        "half_day": new_hd
    }
    
    for k, v in field_map.items():
        old_v = rec.get(k)
        if k in ("punch_in", "punch_out") and old_v is not None:
            old_v = make_aware(old_v)
            
        if old_v != v:
            changes[k] = {"from": old_v, "to": v}
            updates[k] = v
            
    if not changes:
        raise HTTPException(422, detail=[{"loc": ["body"], "msg": "Request changes nothing", "type": "value_error"}])
        
    history_entry = {
        "at": truncate_to_seconds(datetime.now(timezone.utc)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }
    
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "history": {"$size": len(rec.get("history", []))}},
        {
            "$set": updates,
            "$push": {"history": history_entry}
        },
        return_document=pymongo.ReturnDocument.AFTER
    )
    
    if not updated:
        raise HTTPException(409, detail="Concurrent regularization detected")
        
    return format_attendance_doc(updated)
