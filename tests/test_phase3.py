import os
import pytest
from fastapi.testclient import TestClient

os.environ["MONGO_DB"] = "attendance_db_test"

from app.main import app, db

@pytest.fixture(scope="module")
def client():
    db.employees.drop()
    db.attendance_logs.drop()
    with TestClient(app) as client:
        yield client
    db.employees.drop()
    db.attendance_logs.drop()

def setup_data(client):
    db.employees.drop()
    db.attendance_logs.drop()
    
    # 3 employees in Engineering
    # EMP1: 2 logs in 2026-07
    # EMP2: zero logs in 2026-07
    # EMP3: 1 log in 2026-07 (tied late with EMP1)
    
    client.post("/employees", json={
        "emp_code": "EMP01", "name": "Emp 1", "email": "emp1@ex.com",
        "department": "Engineering", "joined_on": "2026-06-01"
    })
    client.post("/employees", json={
        "emp_code": "EMP02", "name": "Emp 2", "email": "emp2@ex.com",
        "department": "Engineering", "joined_on": "2026-07-15" # Mid month
    })
    client.post("/employees", json={
        "emp_code": "EMP03", "name": "Emp 3", "email": "emp3@ex.com",
        "department": "Engineering", "joined_on": "2026-06-01"
    })
    
    db.attendance_logs.insert_many([
        {
            "emp_code": "EMP01",
            "date": "2026-07-06", # Monday
            "status": "PRESENT",
            "late_minutes": 20,
            "overtime_minutes": 0,
            "work_hours": 4.0, # half day
            "half_day": True
        },
        {
            "emp_code": "EMP01",
            "date": "2026-07-07", # Tuesday
            "status": "LEAVE",
            "late_minutes": 0,
            "overtime_minutes": 0,
            "work_hours": None,
            "half_day": False
        },
        {
            "emp_code": "EMP03",
            "date": "2026-07-06",
            "status": "PRESENT",
            "late_minutes": 20, # Tie with EMP01
            "overtime_minutes": 40,
            "work_hours": 9.5,
            "half_day": False
        }
    ])

def test_employee_monthly(client):
    setup_data(client)
    res = client.get("/analytics/employees/EMP01/monthly?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    assert data["working_days"] == 23 # July 2026 has 23 weekdays
    assert data["present_days"] == 0.5
    assert data["leave_days"] == 1
    assert data["late_count"] == 1
    assert data["total_late_minutes"] == 20
    assert data["attendance_pct"] == round((0.5 / 23) * 100, 2)

def test_department_summary(client):
    setup_data(client)
    res = client.get("/analytics/departments/summary?month=2026-07&department=Engineering")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    # EMP02 joined mid-month (2026-07-15) which is <= end of month (2026-07-31), so headcount = 3
    assert item["headcount"] == 3
    # present_days = 0.5 (EMP01) + 1.0 (EMP03)
    assert item["present_days"] == 1.5
    # avg_work_hours = (4.0 + 9.5) / 2 = 13.5 / 2 = 6.75
    assert item["avg_work_hours"] == 6.75
    assert item["leave_count"] == 1
    assert item["late_count"] == 2
    assert item["total_late_minutes"] == 40

def test_late_leaderboard_ties(client):
    setup_data(client)
    res = client.get("/analytics/leaderboard/late?month=2026-07")
    assert res.status_code == 200
    data = res.json()
    items = data["items"]
    assert len(items) == 2
    
    # EMP01 and EMP03 have 20 late_minutes. Standard competition ranking -> rank 1 for both
    # Tied values sorted by emp_code asc
    assert items[0]["rank"] == 1
    assert items[0]["emp_code"] == "EMP01"
    assert items[1]["rank"] == 1
    assert items[1]["emp_code"] == "EMP03"

def test_department_trend(client):
    setup_data(client)
    res = client.get("/analytics/departments/Engineering/trend?from_date=2026-07-01&to_date=2026-07-10")
    assert res.status_code == 200
    items = res.json()["items"]
    
    assert len(items) == 10
    
    # July 6th: hc=2 (EMP1, EMP3 joined 06-01. EMP2 joined 07-15 so hc=2). present=1.5
    jul_6 = next(i for i in items if i["date"] == "2026-07-06")
    assert jul_6["headcount"] == 2
    assert jul_6["present_count"] == 1.5
    assert jul_6["attendance_rate"] == 0.75 # 1.5 / 2
    
    # July 4th (weekend)
    jul_4 = next(i for i in items if i["date"] == "2026-07-04")
    assert jul_4["is_working_day"] is False
    assert jul_4["attendance_rate"] is None
    
    # 7-day moving average on July 6 (first non-null is Jul 6, so avg is just 0.75)
    # wait, moving_avg_7d checks previous 6 docs. 
    # The documents for Jul 1..5 will have None for weekends, but 0.0 for weekdays.
    # Jul 1, 2, 3 are weekdays (0.0). Jul 4, 5 are weekends (None). Jul 6 is weekday (0.75).
    # avg of [0.0, 0.0, 0.0, 0.75] = 0.1875.
    assert jul_6["moving_avg_7d"] == 0.1875

def test_trend_validation_errors(client):
    res = client.get("/analytics/departments/Engineering/trend?from_date=2026-07-10&to_date=2026-07-01")
    assert res.status_code == 422
    
    res2 = client.get("/analytics/departments/Engineering/trend?from_date=2026-01-01&to_date=2026-05-01") # > 92 days
    assert res2.status_code == 422
