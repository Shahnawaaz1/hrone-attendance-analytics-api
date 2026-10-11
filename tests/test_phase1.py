import os
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

# Use a test DB for safe execution
os.environ["MONGO_DB"] = "attendance_db_test"

from app.main import app, db

@pytest.fixture(scope="module")
def client():
    # Setup test DB (drop collections before start)
    db.employees.drop()
    db.attendance_logs.drop()
    
    # Use TestClient with lifespan context
    with TestClient(app) as client:
        yield client
        
    # Teardown test DB
    db.employees.drop()
    db.attendance_logs.drop()

def test_create_employee(client):
    res = client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })
    assert res.status_code == 201
    data = res.json()
    assert data["emp_code"] == "EMP0001"
    assert "created_at" in data
    assert isinstance(data["created_at"], int) # epoch ms
    assert "_id" not in data

def test_create_employee_duplicate(client):
    res = client.post("/employees", json={
        "emp_code": "EMP0001", # Same emp_code
        "name": "Jane Doe",
        "email": "jane@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-06"
    })
    assert res.status_code == 409

def test_create_employee_invalid_shift(client):
    res = client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Bob",
        "email": "bob@example.com",
        "department": "HR",
        "shift_start": "09:30",
        "shift_end": "09:30", # Invalid, must differ
        "joined_on": "2026-01-05"
    })
    assert res.status_code == 422

def test_punch_in(client):
    # Punch in logic uses IST, etc.
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783312500000 # Example epoch ms
    })
    assert res.status_code == 201
    data = res.json()
    assert data["status"] == "PRESENT"
    assert data["emp_code"] == "EMP0001"
    assert isinstance(data["punch_in"], int)
    assert data["late_minutes"] >= 0
    assert "_id" not in data
    assert "id" not in data

def test_punch_in_duplicate(client):
    # Sending same punch_in for the same day (should be determined identically based on punched_at)
    res = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0001",
        "punched_at": 1783312500000 
    })
    assert res.status_code == 409

def test_list_attendance(client):
    res = client.get("/attendance?emp_code=EMP0001")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert len(data["items"]) == 1
    assert data["items"][0]["emp_code"] == "EMP0001"
    assert "_id" not in data["items"][0]
    assert "id" not in data["items"][0]
    assert isinstance(data["items"][0]["punch_in"], int)

def test_list_employees(client):
    res = client.get("/employees")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert "_id" not in data["items"][0]
    assert "id" not in data["items"][0]

def test_pagination_total_counts_filtered_only(client):
    res = client.get("/employees?department=Marketing")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 0 # no Marketing employees
    assert len(data["items"]) == 0

def test_date_validation_in_list_attendance(client):
    res = client.get("/attendance?date_from=2026-02-01&date_to=2026-01-01")
    assert res.status_code == 422
