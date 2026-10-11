# HROne Employee Attendance & Analytics API

This project provides a robust RESTful API for managing and analysing employee attendance, built with FastAPI and MongoDB. It strictly enforces business rules around shift timings, overtime, grace periods, half days, and competition rankings across complex queries.

## Prerequisites
- Python 3.12+
- MongoDB 6.0+ (required for advanced aggregation operators like `$densify` and `$setWindowFields`)

## Setup Instructions

1. **Environment Variables**: Create a `.env` file in the root directory (alongside `app/`):
   ```env
   MONGO_URI="mongodb://localhost:27017"
   MONGO_DB="attendance_db"
   ```

2. **Dependencies**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Or .venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```

3. **Database Configuration**:
   The application idempotently creates all necessary MongoDB indexes (such as unique constraints for concurrent races) during the FastAPI startup event. No manual index creation is required.

4. **Seed Instructions**:
   To populate the database with sample data according to `DATA_MODEL.md`:
   ```bash
   python sample_data/sample_seed.py
   ```
   *Note: Ensure your MongoDB server is running locally before seeding.*

5. **Server Startup**:
   Start the FastAPI server:
   ```bash
   uvicorn app.main:app --port 8000
   ```
   The API will be available at `http://localhost:8000`.

## API Documentation
Once the server is running, you can explore the interactive API documentation and test endpoints at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Running Tests
Tests use PyTest and mock data interacting with a secondary isolated database (`attendance_db_test`) to verify everything correctly without damaging local data.
```bash
python -m pytest tests/ -v
```

## Known Limitations
- The `$densify` aggregation operator used for time-series gap filling in `/analytics/departments/{department}/trend` strictly requires MongoDB v5.1+.
- In a massively scaled environment, dynamically querying the raw `attendance_logs` collection for historical analytics might become slow; pre-aggregation bucket views or materialized views should be adopted as data scales 100x.
