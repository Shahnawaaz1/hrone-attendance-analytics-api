# HROne Employee Attendance & Analytics API

This project provides a robust, high-performance RESTful API for managing and analyzing employee attendance, built with FastAPI and MongoDB. It strictly enforces business rules around shift timings, overtime, grace periods, half days, concurrent state transitions, and competition rankings across complex queries.

## Prerequisites
- Python 3.11+
- MongoDB 6.0+ (required for advanced aggregation operators like `$densify` and `$setWindowFields`)

## Setup Instructions

1. **Environment Variables**: Configure environment variables directly or create a `.env` file in the root directory:
   ```env
   MONGO_URI="mongodb://localhost:27017"
   MONGO_DB="attendance_db"
   ```
   *Note: Environment variables passed via the system environment take precedence over `.env`.*

2. **Virtual Environment & Dependencies**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Database Indexes**:
   The application idempotently creates all necessary MongoDB compound and unique indexes during FastAPI startup (`lifespan` handler):
   - `employees`:
     - `emp_code`: Unique index enforcing employee identifier uniqueness.
     - `department`: Single-field index for department filtering.
     - `joined_on`: Single-field index optimizing headcount active-date cutoffs.
     - `(department, joined_on)`: Compound index optimizing department trend and summary joins.
   - `attendance_logs`:
     - `(emp_code, date)`: Unique compound index preventing duplicate daily check-ins.
     - `(date: -1, emp_code: 1)`: Compound index eliminating collection scans and in-memory sorting on attendance listings.
     - `date`: Single-field index for date-range queries.
     - `(date, late_minutes)`: Compound index accelerating late leaderboard date filtering and aggregation.

4. **Sample Data (Optional)**:
   To populate the database with initial sample records:
   ```bash
   python sample_seed.py
   ```

5. **Server Startup**:
   Start the FastAPI server:
   ```bash
   uvicorn app.main:app --port 8000
   ```
   The API will be available at `http://localhost:8000`.

## API Endpoints & Documentation

Once the server is running, explore the interactive documentation:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`
- OpenAPI Specification: `http://localhost:8000/openapi.json`

### Supported Endpoints:
- `GET /health`: Healthcheck confirming active MongoDB connection.
- `POST /employees`: Register employee with shift and department details.
- `GET /employees`: Paginated employee directory.
- `POST /attendance/punch-in`: Record attendance punch-in with grace-period calculations.
- `POST /attendance/punch-out`: Record punch-out, computing work hours, half days, and overtime.
- `GET /attendance`: Paginated attendance history with date filtering and index-backed sorting.
- `PATCH /attendance/{emp_code}/{date}`: Atomic regularization with immutable audit trail.
- `GET /analytics/employees/{emp_code}/monthly`: Monthly summary of attendance and work hours.
- `GET /analytics/departments/summary`: Department-level attendance metrics (includes employees without logs).
- `GET /analytics/leaderboards/late`: Competition-ranked leaderboard for late minutes.
- `GET /analytics/departments/{department}/trend`: Daily attendance trends with `$densify` gap-filling (`from` and `to` query parameters).
- `GET /admin/explain/{endpoint}`: MongoDB query execution plans (`executionStats`) for performance profiling.

## Running Tests
Run the test suite using pytest:
```bash
pytest tests/ -v
```
All tests run against an isolated test database (`attendance_db_test`) to ensure zero impact on development or production databases.

## Known Limitations
- The `$densify` aggregation operator used for time-series gap filling in `/analytics/departments/{department}/trend` strictly requires MongoDB v5.1+.
- In high-throughput distributed environments (>100x scale), historical analytics pipelines over raw collections should be augmented with pre-aggregated rollups or change streams.
