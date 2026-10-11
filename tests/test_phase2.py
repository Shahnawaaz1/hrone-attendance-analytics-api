import os
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

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

def setup_employee(client, emp_code, shift_start, shift_end):
    client.post("/employees", json={
        "emp_code": emp_code,
        "name": "Test Emp",
        "email": f"{emp_code}@example.com",
        "department": "Engineering",
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": "2026-01-05"
    })

def test_punch_out_success(client):
    setup_employee(client, "EMP_OUT_01", "09:30", "18:30")
    
    # 2026-07-06 10:30:00 IST (05:00:00 UTC) = 1783314000000 ms
    deterministic_ms = 1783314000000
    
    in_res = client.post("/attendance/punch-in", json={"emp_code": "EMP_OUT_01", "punched_at": deterministic_ms})
    assert in_res.status_code == 201
    
    out_res = client.post("/attendance/punch-out", json={"emp_code": "EMP_OUT_01", "punched_at": deterministic_ms + 4*3600*1000})
    assert out_res.status_code == 200
    data = out_res.json()
    assert data["work_hours"] == 4.0
    assert data["half_day"] is True # 4.0 < 4.5
    assert data["overtime_minutes"] == 0

def test_punch_out_missing_punch_in(client):
    setup_employee(client, "EMP_OUT_02", "09:30", "18:30")
    out_res = client.post("/attendance/punch-out", json={"emp_code": "EMP_OUT_02"})
    assert out_res.status_code == 404

def test_punch_out_duplicate(client):
    setup_employee(client, "EMP_OUT_03", "09:30", "18:30")
    deterministic_ms = 1783314000000
    client.post("/attendance/punch-in", json={"emp_code": "EMP_OUT_03", "punched_at": deterministic_ms})
    client.post("/attendance/punch-out", json={"emp_code": "EMP_OUT_03", "punched_at": deterministic_ms + 4*3600*1000})
    
    out_res_dup = client.post("/attendance/punch-out", json={"emp_code": "EMP_OUT_03", "punched_at": deterministic_ms + 5*3600*1000})
    assert out_res_dup.status_code == 409

def test_overnight_shift_and_overtime(client):
    setup_employee(client, "EMP_OVERNIGHT", "22:00", "06:00")
    # Punch in at 21:55 IST on 2026-07-06 -> UTC: 16:25:00Z
    punch_in_ms = 1783355100000 # 2026-07-06T16:25:00Z
    # Punch out at 06:40 IST on 2026-07-07 -> UTC: 01:10:00Z next day
    punch_out_ms = 1783386600000 # 2026-07-07T01:10:00Z
    
    in_res = client.post("/attendance/punch-in", json={"emp_code": "EMP_OVERNIGHT", "punched_at": punch_in_ms})
    assert in_res.status_code == 201
    assert in_res.json()["date"] == "2026-07-06"
    
    out_res = client.post("/attendance/punch-out", json={"emp_code": "EMP_OVERNIGHT", "punched_at": punch_out_ms})
    assert out_res.status_code == 200
    data = out_res.json()
    assert data["work_hours"] == 8.75
    assert data["overtime_minutes"] == 40

def test_half_up_rounding(client):
    setup_employee(client, "EMP_ROUND", "09:30", "18:30")
    # 4 hours and 45 seconds = 4.0125 hours -> round half up -> 4.01
    deterministic_ms = 1783314000000
    client.post("/attendance/punch-in", json={"emp_code": "EMP_ROUND", "punched_at": deterministic_ms})
    out_res = client.post("/attendance/punch-out", json={"emp_code": "EMP_ROUND", "punched_at": deterministic_ms + (4*3600 + 45)*1000})
    assert out_res.json()["work_hours"] == 4.01

def test_regularize_valid_correction(client):
    setup_employee(client, "EMP_REG_01", "09:30", "18:30")
    punch_in_ms = 1783312500000 # say 2026-07-06
    in_res = client.post("/attendance/punch-in", json={"emp_code": "EMP_REG_01", "punched_at": punch_in_ms})
    date = in_res.json()["date"]
    
    reg_res = client.patch(f"/attendance/EMP_REG_01/{date}", json={
        "reason": "forgot to punch out",
        "regularized_by": "admin",
        "punch_out": punch_in_ms + 9*3600*1000
    })
    assert reg_res.status_code == 200
    data = reg_res.json()
    assert data["work_hours"] == 9.0
    assert len(data["history"]) == 1
    assert data["history"][0]["changes"]["punch_out"]["to"] == punch_in_ms + 9*3600*1000
    
def test_regularize_invalid_correction_no_changes(client):
    # setup
    setup_employee(client, "EMP_REG_02", "09:30", "18:30")
    punch_in_ms = 1783312500000
    in_res = client.post("/attendance/punch-in", json={"emp_code": "EMP_REG_02", "punched_at": punch_in_ms})
    date = in_res.json()["date"]
    
    # same values
    reg_res = client.patch(f"/attendance/EMP_REG_02/{date}", json={
        "reason": "nothing changed",
        "regularized_by": "admin",
        "punch_in": punch_in_ms
    })
    assert reg_res.status_code == 422

def test_regularize_absent_clears_times(client):
    setup_employee(client, "EMP_REG_03", "09:30", "18:30")
    punch_in_ms = 1783312500000
    in_res = client.post("/attendance/punch-in", json={"emp_code": "EMP_REG_03", "punched_at": punch_in_ms})
    date = in_res.json()["date"]
    
    reg_res = client.patch(f"/attendance/EMP_REG_03/{date}", json={
        "reason": "was actually absent",
        "regularized_by": "admin",
        "status": "ABSENT"
    })
    assert reg_res.status_code == 200
    assert reg_res.json()["punch_in"] is None
    assert reg_res.json()["work_hours"] is None
