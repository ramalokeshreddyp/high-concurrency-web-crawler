# 🏗 System Architecture & Technical Design Document

## High-Concurrency Web Crawler and Content Analysis Service

---

## 1. Executive Overview & Main Objectives

### 1.1 Objective
The **High-Concurrency Web Crawler and Content Analysis Service** is designed to address the fundamental challenges of modern web data engineering:
1. **Asynchronous Decoupling**: Processing large-scale, long-running web crawls without blocking client HTTP request lifecycles.
2. **High-Throughput Multithreading**: Utilizing concurrent thread pools to maximize network and CPU utilization while avoiding resource saturation and server bans.
3. **Robust Data Consistency & Deduplication**: Ensuring circular hyperlinks and duplicate paths do not trigger infinite loops or redundant database transactions.
4. **Secure Access Governance**: Implementing stateless JSON Web Token (JWT) authentication with granular ownership isolation for submitted crawl operations.
5. **Production-Ready Orchestration**: Employing Docker Compose with explicit dependency chains, active health checks, and isolated networks.

---

## 2. System Architecture & High-Level Design

The system follows a decoupled, service-oriented architecture (SOA) comprising four core tiers:
1. **Client Tier**: Consumers interacting via RESTful HTTP interfaces.
2. **API Tier (FastAPI)**: Stateless control plane handling authentication, validation, queue dispatch, and status polling.
3. **Queue Tier (Redis)**: Durable FIFO message queue buffering jobs between API and execution units.
4. **Worker Tier (Thread Pool Engine)**: Background consumer orchestrating multithreaded crawling, parsing, and relational persistence.
5. **Persistence Tier (PostgreSQL)**: Relational storage of user records, job telemetry, and parsed page results.

### High-Level Component Diagram

```mermaid
graph TD
    Client["Client / Evaluator / Browser"]

    subgraph HostContainer["Docker Application Container ('app')"]
        subgraph APIService["API Service (FastAPI)"]
            AuthMod["Authentication Controller\n(/api/auth)"]
            JobMod["Job Controller\n(/api/jobs)"]
            JWTGate["JWT Bearer Gatekeeper\n(Dependency Injection)"]
            Pydantic["Schema Validator\n(Pydantic v2)"]
        end

        subgraph WorkerService["Worker Engine (Background Daemon)"]
            Listener["Queue Listener\n(Redis BRPOP Consumer)"]
            Coordinator["Crawl Coordinator\n(Thread Pool Manager)"]
            FrontierQ["Frontier Queue\n(queue.Queue FIFO)"]
            VisitedReg["Visited URL Registry\n(Set + Threading Mutex)"]
            ThreadPool["Worker Threads (T1 .. Tn)\nConfigured by WORKER_THREADS"]
            Parser["HTML & Metadata Parser\n(BeautifulSoup4)"]
        end
    end

    subgraph CacheService["Redis Cache ('cache')"]
        Queue["List: crawl_queue\n(FIFO Buffer)"]
    end

    subgraph DBService["PostgreSQL Database ('db')"]
        UsersTbl[("users Table\n(id, email, password_hash)")]
        JobsTbl[("crawl_jobs Table\n(id, user_id, start_url, status)")]
        ResultsTbl[("page_results Table\n(id, job_id, url, title, words)")]
    end

    subgraph TargetWeb["External World Wide Web"]
        TargetPages["Target Domain Pages\n(HTTP / HTTPS)"]
        Robots["robots.txt Policy File"]
    end

    %% Flow connections
    Client -->|"HTTP Requests"| APIService
    AuthMod -->|"Bcrypt Hash Check / Insert"| UsersTbl
    JobMod -.->|"Token Verification"| JWTGate
    JobMod -->|"1. Record PENDING Job"| JobsTbl
    JobMod -->|"2. LPUSH Job Payload"| Queue
    JobMod -->|"HTTP 202 Accepted"| Client

    Listener -->|"BRPOP"| Queue
    Listener -->|"Trigger Job"| Coordinator
    Coordinator -->|"Check Compliance"| Robots
    Coordinator -->|"Seed start_url"| FrontierQ
    Coordinator -->|"UPDATE status=RUNNING"| JobsTbl

    FrontierQ --> ThreadPool
    ThreadPool <-->|"Verify & Mark Visited"| VisitedReg
    ThreadPool -->|"Fetch HTML"| TargetPages
    ThreadPool -->|"Extract Text & Links"| Parser
    Parser -->|"Push Valid Child Links"| FrontierQ
    ThreadPool -->|"Write Result"| ResultsTbl

    Coordinator -->|"UPDATE status=COMPLETED / FAILED"| JobsTbl
```

---

## 3. Key Modules & Subsystem Responsibilities

```mermaid
classDiagram
    class Config {
        +int PORT
        +string HOST
        +string POSTGRES_HOST
        +string REDIS_HOST
        +string REDIS_QUEUE_NAME
        +string JWT_SECRET_KEY
        +int WORKER_THREADS
        +int CRAWL_TIMEOUT_SECONDS
    }

    class DatabaseManager {
        -ThreadedConnectionPool db_pool
        +get_db_pool()
        +get_db()
        +get_db_cursor(commit)
        +init_db()
    }

    class JWTHandler {
        +hash_password(string) string
        +verify_password(string, string) bool
        +create_access_token(dict) string
        +create_refresh_token(dict) string
        +decode_token(string, type) dict
    }

    class AuthRouter {
        +register(RegisterRequest)
        +login(LoginRequest)
        +refresh(RefreshRequest)
        +get_current_user() dict
    }

    class JobRouter {
        +submit_job(JobCreateRequest)
        +list_jobs()
        +get_job(job_id)
        +get_job_results(job_id)
    }

    class QueueWorkerManager {
        -bool _running
        -Thread _thread
        -Redis _redis_client
        +start()
        +stop()
        -_run_loop()
    }

    class JobCrawler {
        +int job_id
        +string start_url
        +int max_depth
        +int num_threads
        +Queue frontier
        +Set visited
        +Lock visited_lock
        +run()
        -_worker_loop()
    }

    Config <-- DatabaseManager
    Config <-- JWTHandler
    DatabaseManager <-- AuthRouter
    JWTHandler <-- AuthRouter
    DatabaseManager <-- JobRouter
    AuthRouter <-- JobRouter : Protected by Bearer
    QueueWorkerManager --> JobCrawler : Spawns
    JobCrawler --> DatabaseManager : Persists Results
```

### Module Descriptions
1. **`app.config`**:
   - Centralizes environment variable extraction with deterministic type casting.
   - Provides defaults that maintain identical behavior across containerized and bare-metal environments.
2. **`app.database`**:
   - Manages a thread-safe PostgreSQL connection pool (`psycopg2.pool.ThreadedConnectionPool`).
   - Implements context managers (`get_db`, `get_db_cursor`) ensuring automatic rollback on exceptions and preventing connection starvation.
   - Idempotent `init_db()` runs on application startup to ensure DDL schemas and evaluation seed users exist.
3. **`app.auth`**:
   - `jwt_handler.py`: Implements salted adaptive bcrypt hashing and cryptographic JWT token encoding/decoding with strict payload signature checks and expiry validation.
   - `routes.py`: Encapsulates registration, login, token refresh endpoints, and the `get_current_user` FastAPI dependency.
4. **`app.jobs`**:
   - Validates requests via Pydantic schemas.
   - Enforces user isolation: queries guarantee users can only read job telemetry and results matching their authenticated `user_id`.
   - Serializes crawl directives and enqueues them onto Redis.
5. **`app.worker`**:
   - `queue_worker.py`: Runs a daemon thread listening on `BRPOP crawl_queue`. Handles graceful SIGINT/SIGTERM teardown.
   - `crawler.py`: Orchestrates multi-threaded crawling, frontier synchronization, `robots.txt` enforcement, and domain boundary filtering.

---

## 4. End-to-End Workflow & Execution Flow

### Data Lifecycle State Transition

```mermaid
stateDiagram-v2
    [*] --> PENDING: Client submits POST /api/jobs
    PENDING --> RUNNING: Worker dequeues job from Redis
    
    state RUNNING {
        [*] --> CheckRobots: Fetch /robots.txt
        CheckRobots --> SeedFrontier: Allowed
        CheckRobots --> Abort: Disallowed (User-agent: * Disallow: /)
        
        state SeedFrontier {
            [*] --> FrontierQueue: Push (start_url, depth=0)
        }
        
        state CrawlCycle {
            FrontierQueue --> FetchPage: Worker thread pops URL
            FetchPage --> ParseContent: HTTP 200 OK
            FetchPage --> HandleError: HTTP 4xx/5xx or Network Fail
            
            ParseContent --> StorePage: Title & Word Count
            StorePage --> DiscoverLinks: If current_depth < max_depth
            DiscoverLinks --> FilterLinks: Same Domain & Not in Visited
            FilterLinks --> FrontierQueue: Push (new_url, depth+1)
        }
    }
    
    Abort --> FAILED: Update crawl_jobs with reason
    HandleError --> FAILED: If start_url fails
    CrawlCycle --> COMPLETED: Frontier empty & all threads idle
    
    FAILED --> [*]
    COMPLETED --> [*]
```

### Multithreaded Crawl Worker Synchronization

```mermaid
flowchart TD
    StartCrawl(["Start Job Execution"]) --> UpdateRunning["Set Status = RUNNING"]
    UpdateRunning --> CheckRobots{"robots.txt Disallow: / ?"}
    CheckRobots -- Yes --> MarkFail["Set Status = FAILED (Robots Disallowed)"]
    CheckRobots -- No --> SeedInit["visited.add(seed)\nfrontier.put((seed, 0))"]

    SeedInit --> SpawnThreads["Spawn WORKER_THREADS"]

    subgraph ThreadExecutionLoop["Concurrent Worker Loop (x WORKER_THREADS)"]
        GetItem["Get (url, depth) from Frontier"]
        GetItem --> CheckSentinel{"Item is None?"}
        CheckSentinel -- Yes --> ExitThread["task_done() & Exit Loop"]
        CheckSentinel -- No --> FetchHTTP["HTTP GET Request (with Timeout)"]
        
        FetchHTTP --> EvalHTTP{"Status < 400?"}
        EvalHTTP -- No --> CheckSeedErr{"depth == 0?"}
        CheckSeedErr -- Yes --> FlagSeedErr["Set start_url_failed = True"]
        CheckSeedErr -- No --> TaskDone1["frontier.task_done()"]
        FlagSeedErr --> TaskDone1
        
        EvalHTTP -- Yes --> ParseHTML["Extract Title, Body Text, Links"]
        ParseHTML --> SaveDB["INSERT INTO page_results (Thread-Safe DB Pool)"]
        SaveDB --> DepthCheck{"depth < max_depth?"}
        
        DepthCheck -- No --> TaskDone2["frontier.task_done()"]
        DepthCheck -- Yes --> FilterDomain["Normalize Links & Filter Base Domain"]
        FilterDomain --> LockVisited["Acquire visited_lock"]
        LockVisited --> CheckVisited{"Link in visited?"}
        CheckVisited -- Yes --> ReleaseLock1["Release Lock"]
        CheckVisited -- No --> AddVisited["visited.add(link)\nfrontier.put((link, depth + 1))"]
        AddVisited --> ReleaseLock1
        ReleaseLock1 --> TaskDone2
    end

    SpawnThreads --> ThreadExecutionLoop
    TaskDone1 --> FrontierDrain{"All Items Processed?\n(frontier.join())"}
    TaskDone2 --> FrontierDrain

    FrontierDrain --> SendSentinels["Push 'None' Sentinels to Workers"]
    SendSentinels --> JoinThreads["Wait for Threads to Terminate"]
    JoinThreads --> CheckOutcome{"start_url_failed or 0 results?"}
    CheckOutcome -- Yes --> SetFailed["UPDATE status = FAILED"]
    CheckOutcome -- No --> SetCompleted["UPDATE status = COMPLETED"]
    SetFailed --> EndCrawl(["End Job Execution"])
    SetCompleted --> EndCrawl
```

---

## 5. Database Schema & Entity Relationships

The relational model enforces strict referential integrity with foreign key cascades:

```mermaid
erDiagram
    users ||--o{ crawl_jobs : "submits"
    crawl_jobs ||--o{ page_results : "produces"

    users {
        int id PK "SERIAL"
        string email "VARCHAR(255) UNIQUE NOT NULL"
        string password_hash "VARCHAR(255) NOT NULL"
        timestamp created_at "DEFAULT CURRENT_TIMESTAMP"
    }

    crawl_jobs {
        int id PK "SERIAL"
        int user_id FK "REFERENCES users(id) ON DELETE CASCADE"
        text start_url "NOT NULL"
        int depth "DEFAULT 0 NOT NULL"
        string status "VARCHAR(50) DEFAULT 'PENDING'"
        text error_message "NULLABLE"
        timestamp created_at "DEFAULT CURRENT_TIMESTAMP"
        timestamp updated_at "DEFAULT CURRENT_TIMESTAMP"
    }

    page_results {
        int id PK "SERIAL"
        int job_id FK "REFERENCES crawl_jobs(id) ON DELETE CASCADE"
        text url "NOT NULL"
        text title "NULLABLE"
        int word_count "DEFAULT 0 NOT NULL"
        timestamp created_at "DEFAULT CURRENT_TIMESTAMP"
    }
```

### Indexing Strategy
- **`idx_crawl_jobs_user_id`**: Accelerates filtered queries for authenticated user job listings.
- **`idx_crawl_jobs_status`**: Optimizes status-based lookups and telemetry monitoring.
- **`idx_page_results_job_id`**: Accelerates result retrieval for `GET /api/jobs/{id}/results`.

---

## 6. Concurrency, Race Condition Mitigation & Safety

### Concurrency Primitives Matrix

| Component | Mechanism | Purpose / Race Condition Mitigated |
|---|---|---|
| **Crawl Frontier** | `queue.Queue` | Native thread-safe FIFO queue with condition variables (`not_empty`, `not_full`) preventing race conditions during item insertion and retrieval. |
| **Task Completion** | `queue.Queue.join()` | Atomic counter (`unfinished_tasks`) ensures the orchestrator waits until all extracted child URLs have called `task_done()`. |
| **Visited Registry** | `threading.Lock()` | Mutex protects `visited` set modifications. Ensures no two worker threads can simultaneously inspect and insert the same candidate URL. |
| **Database Pool** | `psycopg2.pool.ThreadedConnectionPool` | Dedicated connection instances allocated per thread execution, preventing cross-thread socket corruption. |
| **Queue Worker** | Redis `BRPOP` | Blocking dequeue eliminates busy-wait polling loops, saving CPU cycles while providing instant task dispatch. |

---

## 7. Technology Stack Evaluation & Rationale

```mermaid
pie title Technology Stack Breakdown
    "FastAPI (REST & ASGI)" : 25
    "Python Threading Engine" : 30
    "PostgreSQL 15 (ACID Persistence)" : 20
    "Redis 7 (In-Memory Queue)" : 15
    "Docker Compose (Orchestration)" : 10
```

### Architectural Trade-Off Analysis

| Dimension | Selected Approach | Alternative Evaluated | Why Selected Was Chosen |
|---|---|---|---|
| **API Framework** | FastAPI (Python) | Express.js (Node.js) / Spring Boot | Built-in ASGI async execution, automated Swagger/OpenAPI docs, Pydantic type safety, and tight Python crawler integration. |
| **Worker Concurrency** | Python Thread Pool (`threading`) | Asyncio Event Loop (`aiohttp`) | Threaded pools provide true thread-isolated blocking HTTP pipelines with fine-grained worker thread ID auditing without event loop pollution. |
| **Message Broker** | Redis (`List` FIFO) | RabbitMQ / Apache Kafka | Redis provides sub-millisecond dispatch, minimal memory footprint, zero broker configuration overhead, and robust `BRPOP` semantics. |
| **Database** | PostgreSQL 15 | MongoDB | Relational integrity, foreign key cascades on user deletion, and strict transactional consistency for job state transitions. |

---

## 8. Strengths, Weaknesses, Pros & Cons

### Pros & Advantages
- **Strict Decoupling**: API availability is unaffected by long-running crawl executions.
- **Predictable Resource Allocation**: Configurable `WORKER_THREADS` prevents CPU thrashing and socket exhaustion.
- **Deterministic Deduplication**: In-memory mutex registry eliminates infinite loops across complex web topologies.
- **Zero-Downtime Resilience**: Health checks for all services ensure startup dependencies are satisfied before incoming traffic arrives.
- **Auditable Concurrency**: Every crawl operation logs thread names and system Thread IDs (`TID`) for tracing.

### Cons & Mitigations
- **Process Memory Limits**: In-memory visited sets grow with crawl depth.
  - *Mitigation*: Base-domain restriction limits external link expansion; can be offloaded to Redis Sets (`SADD`) for multi-million URL workloads.
- **Dynamic JavaScript Content**: Relies on static HTML parsing with BeautifulSoup.
  - *Mitigation*: Highly performant for 90%+ of standard web crawling. Headless browsers (e.g. Playwright) can be added as an alternate fetcher backend if client-side hydration is needed.

---

## 9. Crucial Integration & Orchestration Details

### Docker Compose Service Dependency Flow

```mermaid
flowchart LR
    subgraph StartupOrdering["Startup & Health Order"]
        DB["PostgreSQL Service ('db')\nHealthcheck: pg_isready"]
        Cache["Redis Service ('cache')\nHealthcheck: redis-cli ping"]
        App["App Service ('app')\nHealthcheck: curl /health"]
    end

    DB -->|"healthy"| App
    Cache -->|"healthy"| App
```

1. **Service `db`**:
   - Boots PostgreSQL 15 on port 5432.
   - Mounts `db/init.sql` to `/docker-entrypoint-initdb.d/init.sql` for automated zero-configuration database migration and credential seeding.
   - Active healthcheck: `pg_isready -U postgres -d crawler_db`.
2. **Service `cache`**:
   - Boots Redis 7 on port 6379.
   - Active healthcheck: `redis-cli ping`.
3. **Service `app`**:
   - Waits for both `db` and `cache` to transition to `healthy`.
   - Executes `uvicorn app.main:app` within an unprivileged lightweight container.
   - Active healthcheck: `curl -f http://localhost:8000/health`.
