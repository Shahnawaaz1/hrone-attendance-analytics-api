import os
import pytest
from fastapi.testclient import TestClient

os.environ["MONGO_DB"] = "attendance_db_test"

from app.main import app, db

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as client:
        yield client

def test_explain_attendance_list(client):
    res = client.get("/admin/explain/attendance_list")
    assert res.status_code == 200
    data = res.json()
    assert data["endpoint"] == "attendance_list"
    assert data["collection"] == "attendance_logs"
    assert "explain" in data

def test_explain_employee_monthly(client):
    res = client.get("/admin/explain/employee_monthly?emp_code=E1&month=2026-07")
    assert res.status_code == 200

def test_explain_missing_params(client):
    res = client.get("/admin/explain/employee_monthly")
    assert res.status_code == 422

def test_explain_invalid_endpoint(client):
    res = client.get("/admin/explain/unknown")
    assert res.status_code == 422

def test_explain_department_trend(client):
    res = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10")
    assert res.status_code == 200
    assert res.json()["collection"] == "1"
