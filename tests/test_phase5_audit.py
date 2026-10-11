import os
import concurrent.futures
import pytest
from fastapi.testclient import TestClient

os.environ["MONGO_DB"] = "attendance_db_test"

from app.main import app, db, init_db_indexes

@pytest.fixture(scope="module")
def client():
    db.employees.drop()
    db.attendance_logs.drop()
    init_db_indexes()
    with TestClient(app) as client:
        yield client
    db.employees.drop()
    db.attendance_logs.drop()
    init_db_indexes()

def test_late_minute_grace_period_boundaries(client):
    db.employees.drop()
    db.attendance_logs.drop()
    init_db_indexes()
    client.post("/employees", json={
        "emp_code": "EMP_LATE_01",
        "name": "Late Test",
        "email": "late@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    
    # Base: 2026-07-06 09:30:00 IST = 04:00:00 UTC = 1783310400000 ms
    # 09:40:00 IST (exactly 10 min grace period): 1783310400000 + 600*1000 = 1783311000000 ms -> late_minutes = 0
    res1 = client.post("/attendance/punch-in", json={"emp_code": "EMP_LATE_01", "punched_at": 1783311000000})
    assert res1.status_code == 201
    assert res1.json()["late_minutes"] == 0
    
    # 09:40:01 IST (10 min 1 sec): next day 2026-07-07 -> 1783396800000 + 601*1000 = 1783397401000 ms -> late_minutes = 10
    res2 = client.post("/attendance/punch-in", json={"emp_code": "EMP_LATE_01", "punched_at": 1783397401000})
    assert res2.status_code == 201
    assert res2.json()["late_minutes"] == 10
    
    # 10:15:59 IST (45 min 59 sec): 2026-07-08 -> 1783483200000 + (45*60 + 59)*1000 = 1783485959000 ms -> late_minutes = 45
    res3 = client.post("/attendance/punch-in", json={"emp_code": "EMP_LATE_01", "punched_at": 1783485959000})
    assert res3.status_code == 201
    assert res3.json()["late_minutes"] == 45

def test_legacy_record_regularization_missing_history_and_half_day(client):
    from datetime import datetime, timezone
    db.employees.drop()
    db.attendance_logs.drop()
    init_db_indexes()
    client.post("/employees", json={
        "emp_code": "EMP_LEGACY",
        "name": "Legacy Emp",
        "email": "legacy@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    
    # Insert a document without 'history' and without 'half_day' (Sample 9 in DATA_MODEL.md)
    db.attendance_logs.insert_one({
        "emp_code": "EMP_LEGACY",
        "date": "2026-07-14",
        "status": "PRESENT",
        "punch_in": datetime(2026, 7, 14, 5, 0, 0, tzinfo=timezone.utc),
        "punch_out": datetime(2026, 7, 14, 12, 30, 0, tzinfo=timezone.utc),
        "work_hours": 7.5,
        "late_minutes": 50,
        "overtime_minutes": 0
    })
    
    # 1. Fetching the legacy record should normalize response with half_day=False and history=[]
    get_res = client.get("/attendance?emp_code=EMP_LEGACY")
    assert get_res.status_code == 200
    rec = get_res.json()["items"][0]
    assert rec["half_day"] is False
    assert rec["history"] == []
    
    # 2. Regularizing this legacy record (e.g. correcting punch_out) must succeed (200, not 409)
    # New punch_out: 18:30 IST (13:00 UTC) = 1784034600000 ms
    patch_res = client.patch("/attendance/EMP_LEGACY/2026-07-14", json={
        "reason": "corrected biometric punchout",
        "regularized_by": "hr.lead",
        "punch_out": 1784034600000
    })
    assert patch_res.status_code == 200
    data = patch_res.json()
    assert len(data["history"]) == 1
    # Ensure NO phantom change for half_day was recorded in audit changes
    changes = data["history"][0]["changes"]
    assert "half_day" not in changes
    assert "punch_out" in changes
    assert "work_hours" in changes

def test_pagination_and_limit_validation(client):
    # page=0 -> 422
    assert client.get("/employees?page=0").status_code == 422
    assert client.get("/attendance?page=0").status_code == 422
    # page_size > 100 -> 422
    assert client.get("/employees?page_size=101").status_code == 422
    assert client.get("/attendance?page_size=101").status_code == 422
    # leaderboard limit > 50 -> 422
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=51").status_code == 422
    assert client.get("/analytics/leaderboard/late?month=2026-07&limit=0").status_code == 422

def test_timestamp_epoch_millis_strict_validation(client):
    db.employees.drop()
    init_db_indexes()
    client.post("/employees", json={
        "emp_code": "EMP_TS_01",
        "name": "Timestamp Test",
        "email": "ts@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    # Seconds-looking value (< 100000000000) -> 422
    res_sec = client.post("/attendance/punch-in", json={"emp_code": "EMP_TS_01", "punched_at": 1783312500})
    assert res_sec.status_code == 422
    
    # Float value -> 422
    res_flt = client.post("/attendance/punch-in", json={"emp_code": "EMP_TS_01", "punched_at": 1783312500000.5})
    assert res_flt.status_code == 422
    
    # String value -> 422
    res_str = client.post("/attendance/punch-in", json={"emp_code": "EMP_TS_01", "punched_at": "1783312500000"})
    assert res_str.status_code == 422

def test_department_trend_openapi_params(client):
    db.employees.drop()
    init_db_indexes()
    client.post("/employees", json={
        "emp_code": "EMP_TR_01",
        "name": "Trend Test",
        "email": "trend@example.com",
        "department": "Operations",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    # Calling using exact openapi parameter names 'from' and 'to'
    res = client.get("/analytics/departments/Operations/trend?from=2026-07-01&to=2026-07-05")
    assert res.status_code == 200
    data = res.json()
    assert data["department"] == "Operations"
    assert len(data["items"]) == 5

def test_explain_all_five_endpoints_with_execution_stats(client):
    db.employees.drop()
    db.attendance_logs.drop()
    init_db_indexes()
    client.post("/employees", json={
        "emp_code": "EMP_EXP_01",
        "name": "Explain Test",
        "email": "exp@example.com",
        "department": "HR",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    
    endpoints = [
        "/admin/explain/attendance_list",
        "/admin/explain/employee_monthly?emp_code=EMP_EXP_01&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=HR&from=2026-07-01&to=2026-07-05"
    ]
    for url in endpoints:
        res = client.get(url)
        assert res.status_code == 200
        data = res.json()
        assert "explain" in data
        assert data["endpoint"] in url

def test_concurrent_punch_in(client):
    db.employees.drop()
    db.attendance_logs.drop()
    init_db_indexes()
    client.post("/employees", json={
        "emp_code": "EMP_CONC_01",
        "name": "Conc Test",
        "email": "conc@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    
    punch_ms = 1783314000000
    
    def attempt_punch_in():
        return client.post("/attendance/punch-in", json={
            "emp_code": "EMP_CONC_01",
            "punched_at": punch_ms
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(attempt_punch_in) for _ in range(10)]
        results = [f.result() for f in futures]

    assert results.count(201) == 1
    assert results.count(409) == 9

def test_concurrent_punch_out(client):
    db.employees.drop()
    db.attendance_logs.drop()
    init_db_indexes()
    client.post("/employees", json={
        "emp_code": "EMP_CONC_02",
        "name": "Conc Test 2",
        "email": "conc2@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    
    in_ms = 1783314000000
    out_ms = in_ms + 4 * 3600 * 1000
    
    client.post("/attendance/punch-in", json={"emp_code": "EMP_CONC_02", "punched_at": in_ms})

    def attempt_punch_out():
        return client.post("/attendance/punch-out", json={
            "emp_code": "EMP_CONC_02",
            "punched_at": out_ms
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(attempt_punch_out) for _ in range(10)]
        results = [f.result() for f in futures]

    assert results.count(200) == 1
    assert results.count(409) == 9

def test_concurrent_regularization(client):
    db.employees.drop()
    db.attendance_logs.drop()
    init_db_indexes()
    client.post("/employees", json={
        "emp_code": "EMP_CONC_03",
        "name": "Conc Test 3",
        "email": "conc3@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-01"
    })
    
    in_ms = 1783314000000
    client.post("/attendance/punch-in", json={"emp_code": "EMP_CONC_03", "punched_at": in_ms})
    
    def attempt_regularize(idx):
        return client.patch("/attendance/EMP_CONC_03/2026-07-06", json={
            "reason": f"concurrent update {idx}",
            "regularized_by": f"admin_{idx}",
            "punch_out": in_ms + (4 + idx) * 3600 * 1000
        }).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(attempt_regularize, i) for i in range(1, 6)]
        results = [f.result() for f in futures]

    # At least one request succeeds with 200, any conflicting concurrent request gets 409
    assert 200 in results
    assert all(code in (200, 409) for code in results)
    
    # Verify history integrity: all successful requests appended exactly one history entry
    get_res = client.get("/attendance?emp_code=EMP_CONC_03")
    rec = get_res.json()["items"][0]
    assert len(rec["history"]) == results.count(200)
