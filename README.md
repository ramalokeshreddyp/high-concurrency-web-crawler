# High-Concurrency Web Crawler and Content Analysis Service

A robust, multi-service backend application designed to crawl websites concurrently, analyze HTML content, and persistently store extracted metadata. Features JWT-based user authentication, asynchronous job queuing with Redis, thread-safe multithreaded crawling with domain restriction and depth control, and full containerization via Docker Compose.

---

## 1. System Architecture

```
                       +-----------------------------+
                       |        Client User          |
                       |  (curl / Postman / Tests)   |
                       +--------------+--------------+
                                      |
                      JWT Auth & API  |  Status / Results Polling
                         Requests     |       (JWT)
                                      v
                       +-----------------------------+
                       |     FastAPI Service         |
                       |    (REST Endpoints)         |
                       +-------+-------------+-------+
                               |             |
                   Enqueue Job |             | Write Job (PENDING)
                   (LPUSH)     |             | Auth / User Verification
                               v             v
                    +--------------+    +-------------------+
                    | Redis Queue  |    | PostgreSQL DB     |
                    | (crawl_queue)|    | (users, jobs,     |
                    +-------+------+    |  page_results)    |
                            |           +---------+---------+
                    BRPOP   |                     ^
                    Job     v                     |
                       +--------------------------+--+
                       |  Multithreaded Worker Pool  |
                       |  - Configurable Threads     |
                       |  - Robots.txt Compliance    |
                       |  - URL Deduplication        |
                       |  - Same-Domain Extraction   |
                       |  - Title & Word Count Parse |
                       +--------------+--------------+
                                      |
                                      v
                                  [Internet]
```

### Core Components
1. **API Service (`app`)**:
   - Built with FastAPI, running on Uvicorn.
   - Handles user registration, login, and JWT token refresh.
   - Provides protected endpoints for submitting crawl jobs and querying metadata/results.
   - Healthcheck endpoint at `/health` and `/api/health`.

2. **Job Queue (`cache`)**:
   - Powered by Redis.
   - Uses `LPUSH` on the API side and `BRPOP` on the worker listener side for reliable asynchronous decoupling.

3. **Multithreaded Worker Pool**:
   - Runs in the background of the application service.
   - Coordinates crawling using a configurable thread pool controlled by `WORKER_THREADS`.
   - Utilizes a synchronized FIFO `queue.Queue` for the crawl frontier and a thread-safe `visited` set for URL deduplication.
   - Respects depth constraints (`depth: 0` crawls only seed, `depth: 1` crawls seed + direct links).
   - Enforces same-domain boundary filtering and checks `robots.txt` before crawling.
   - Persists extracted page titles and word counts to PostgreSQL.

4. **Persistent Storage (`db`)**:
   - PostgreSQL 15 database storing `users`, `crawl_jobs`, and `page_results` tables.
   - Automated database initialization and seeding of test credentials via `db/init.sql`.

---

## 2. Directory Structure

```
├── .env.example              # Documented environment variables template
├── .gitignore                # Git ignore patterns
├── Dockerfile                # Multi-stage/slim Docker container definition
├── README.md                 # Complete system documentation
├── docker-compose.yml        # Multi-service orchestration with healthchecks
├── requirements.txt          # Python dependencies
├── submission.json           # Evaluation test credentials
├── db/
│   └── init.sql              # PostgreSQL DDL schema and test user seed
└── app/
    ├── __init__.py
    ├── config.py             # Configuration loader
    ├── database.py           # Thread-safe connection pool & DB helpers
    ├── main.py               # FastAPI application entrypoint & lifespan
    ├── auth/
    │   ├── __init__.py
    │   ├── jwt_handler.py    # Bcrypt & JWT token utilities
    │   └── routes.py         # Registration, login, refresh, & auth middleware
    ├── jobs/
    │   ├── __init__.py
    │   ├── models.py         # Pydantic schemas for requests & responses
    │   └── routes.py         # Job submission, status, and results endpoints
    └── worker/
        ├── __init__.py
        ├── crawler.py        # Multithreaded crawler engine & content parser
        └── queue_worker.py   # Redis queue BRPOP background manager
```

---

## 3. Environment Configuration

All settings are configured via environment variables. Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

| Variable | Default Value | Description |
|---|---|---|
| `PORT` | `8000` | Port for the API application |
| `HOST` | `0.0.0.0` | Bind host for API server |
| `POSTGRES_HOST` | `db` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_USER` | `postgres` | PostgreSQL username |
| `POSTGRES_PASSWORD` | `postgres` | PostgreSQL password |
| `POSTGRES_DB` | `crawler_db` | PostgreSQL database name |
| `REDIS_HOST` | `cache` | Redis host |
| `REDIS_PORT` | `6379` | Redis port |
| `REDIS_QUEUE_NAME` | `crawl_queue` | Redis queue list name |
| `JWT_SECRET_KEY` | `supersecret...` | Secret key for signing JWTs |
| `JWT_ALGORITHM` | `HS256` | JWT signing algorithm |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access token lifespan |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Refresh token lifespan |
| `WORKER_THREADS` | `4` | Worker threads per crawl job |
| `CRAWL_TIMEOUT_SECONDS` | `10` | HTTP request timeout per page |

---

## 4. Running the Application

### Using Docker Compose (Recommended)

Start all services (`app`, `db`, `cache`):
```bash
docker-compose up --build
```

To stop all services:
```bash
docker-compose down
```

### Healthcheck Verification

All three services implement active healthchecks:
- `db`: `pg_isready -U postgres -d crawler_db`
- `cache`: `redis-cli ping`
- `app`: `curl -f http://localhost:8000/health`

Inspect health status:
```bash
docker-compose ps
```

---

## 5. API Reference & Contracts

### 5.1 Authentication Endpoints

#### Register User
- **Endpoint**: `POST /api/auth/register`
- **Request**:
  ```json
  {
    "email": "user@example.com",
    "password": "password123"
  }
  ```
- **Responses**:
  - `201 Created`:
    ```json
    {
      "id": 1,
      "email": "user@example.com"
    }
    ```
  - `409 Conflict`: If email is already registered.

#### Login User
- **Endpoint**: `POST /api/auth/login`
- **Request**:
  ```json
  {
    "email": "user@example.com",
    "password": "password123"
  }
  ```
- **Responses**:
  - `200 OK`:
    ```json
    {
      "accessToken": "eyJhbGciOi...",
      "refreshToken": "eyJhbGciOi..."
    }
    ```
  - `401 Unauthorized`: Invalid credentials.

#### Refresh Access Token
- **Endpoint**: `POST /api/auth/refresh`
- **Request**:
  ```json
  {
    "refreshToken": "eyJhbGciOi..."
  }
  ```
- **Responses**:
  - `200 OK`:
    ```json
    {
      "accessToken": "eyJhbGciOi..."
    }
    ```
  - `401 Unauthorized`: Invalid or expired refresh token.

---

### 5.2 Crawl Job Endpoints (Protected by JWT)

All job endpoints require the `Authorization: Bearer <accessToken>` header. Missing or invalid tokens return `401 Unauthorized`.

#### Submit a Crawl Job
- **Endpoint**: `POST /api/jobs`
- **Headers**: `Authorization: Bearer <accessToken>`
- **Request**:
  ```json
  {
    "start_url": "https://example.com",
    "depth": 1
  }
  ```
- **Response**:
  - `202 Accepted`:
    ```json
    {
      "job_id": 1,
      "status": "PENDING",
      "message": "Job accepted for processing"
    }
    ```

#### Get Job Status & Metadata
- **Endpoint**: `GET /api/jobs/{job_id}`
- **Headers**: `Authorization: Bearer <accessToken>`
- **Responses**:
  - `200 OK`:
    ```json
    {
      "id": 1,
      "start_url": "https://example.com",
      "depth": 1,
      "status": "COMPLETED",
      "error_message": null,
      "created_at": "2026-11-07T05:00:00Z",
      "updated_at": "2026-11-07T05:00:15Z"
    }
    ```
  - `403 Forbidden` / `404 Not Found`: If job does not belong to the authenticated user.

#### Get Crawl Results
- **Endpoint**: `GET /api/jobs/{job_id}/results`
- **Headers**: `Authorization: Bearer <accessToken>`
- **Responses**:
  - `200 OK`:
    ```json
    [
      {
        "url": "https://example.com",
        "title": "Example Domain",
        "word_count": 52
      }
    ]
    ```
  - `403 Forbidden` / `404 Not Found`: If job is inaccessible.

---

## 6. Concurrency, Multithreading & Safety Details

1. **Frontier & Worker Synchronization**:
   - Uses `queue.Queue` for the crawl frontier. Items are tuples of `(url, current_depth)`.
   - `queue.Queue.join()` cleanly blocks until all tasks in the frontier and their downstream links have invoked `task_done()`.
   - Sentinel `None` items cleanly terminate worker threads upon completion.

2. **Deduplication (`visited` Set)**:
   - Synchronized by a `threading.Lock()` to prevent race conditions when multiple threads discover the same URL simultaneously.
   - URLs are normalized (fragment stripped, case-normalized domain, resolved relative paths) before check and insertion.

3. **Database Connection Pool**:
   - `psycopg2.pool.ThreadedConnectionPool` manages connections safely across threads, avoiding shared connection corruption.

4. **Domain Boundary Control**:
   - New links are filtered to only match the base host domain of the `start_url`, preventing the crawler from wandering off-site.

5. **Depth Limiting**:
   - `depth: 0`: Crawls only `start_url`.
   - `depth: 1`: Crawls `start_url` and directly linked pages.

6. **Robots.txt & Error Handling**:
   - Inspects `/robots.txt` for `User-agent: *` with `Disallow: /`. If blocked, updates job status to `FAILED`.
   - Unreachable or invalid start URLs gracefully fail the job with a descriptive `error_message`.
