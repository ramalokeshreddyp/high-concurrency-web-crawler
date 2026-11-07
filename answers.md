# Questionnaire Answers

### High-Concurrency Web Crawler and Content Analysis Service

---

### Question 1: Describe the concurrency model you chose (e.g., thread pool, actor model) and explain the mechanisms you used to ensure thread safety when accessing shared resources.

**Response:**

I implemented a **fixed-size thread pool** concurrency model combined with an **asynchronous decoupled producer-consumer queue**. 

To ensure safety when accessing shared resources across concurrent threads, I used the following mechanisms:

1. **Thread-Safe Crawl Frontier (`queue.Queue`)**:
   - The crawl frontier is implemented using Python's standard `queue.Queue()`, which internally uses threading `Lock` and `Condition` variables (`not_empty`, `not_full`) to guarantee atomic `put()` and `get()` operations without race conditions.
   - Barrier synchronization is coordinated via `frontier.join()`, which monitors the internal atomic counter of `unfinished_tasks`. This cleanly blocks the coordinator until all popped URLs and any newly discovered child links have invoked `frontier.task_done()`.

2. **Mutex-Protected URL Deduplication Registry (`threading.Lock`)**:
   - To prevent multiple threads from crawling the same URL concurrently or falling into infinite circular loops (e.g., Page A linking to Page B, and Page B linking to Page A), an in-memory set (`self.visited`) is protected by a dedicated mutex lock (`self.visited_lock = threading.Lock()`).
   - Before queueing newly discovered URLs, worker threads must acquire this lock, verify `if norm_link not in self.visited: self.visited.add(norm_link); self.frontier.put(...)`, and then release the lock. This critical section guarantees that no URL is added to the frontier or visited more than once.

3. **Isolated Thread-Safe Database Connection Pooling**:
   - Sharing a single PostgreSQL connection across concurrent threads causes protocol errors and transaction corruption. To ensure complete safety, database access is mediated by `psycopg2.pool.ThreadedConnectionPool`.
   - Each worker thread borrows an isolated connection from the pool via the `get_db_cursor(commit=True)` context manager and returns it immediately after executing its transaction, ensuring thread isolation and zero connection leaks.

4. **Synchronized State & Error Counters**:
   - State flags such as `start_url_failed` and `failure_reason` are synchronized via `self.failure_lock = threading.Lock()`, while the global counter for parsed results is protected by `self.results_lock = threading.Lock()`.

5. **Graceful Worker Teardown via Sentinels**:
   - Once `frontier.join()` unblocks, the coordinator injects `WORKER_THREADS` sentinel values (`None`) into the queue. Each worker thread cleanly detects `None`, calls `frontier.task_done()`, and terminates its execution loop.

---

### Question 2: How would you scale the worker service to handle thousands of concurrent crawl jobs? Discuss potential bottlenecks and your proposed solutions.

**Response:**

The main bottlenecks when scaling to thousands of concurrent crawl jobs would be:

1. **Single-Process Resource Contention & GIL Limitations**:
   - Currently, the worker manager runs as a daemon thread pool inside the same container as the FastAPI application. Thousands of jobs would spawn tens of thousands of OS threads, leading to memory exhaustion, excessive thread context-switching overhead, and Python GIL contention during HTML parsing.
2. **Redis Single-List Queue Contention**:
   - A single Redis List (`crawl_queue`) with sequential `LPUSH` and `BRPOP` operations can become a contention point under thousands of concurrent dispatches.
3. **Database Connection Pool & I/O Saturation**:
   - Each thread borrowing a connection for single-row inserts into `page_results` would rapidly exhaust PostgreSQL's `max_connections` limit and bottleneck on write disk I/O (WAL writes and table locking).
4. **In-Memory Deduplication Memory Footprint**:
   - Storing visited sets in process memory for thousands of deep crawl jobs can exhaust host RAM.
5. **Target Website Rate Limiting and Network Egress Limits**:
   - High-throughput crawling risks hitting target server rate limits (HTTP 429), triggering IP blocks, or saturating host network bandwidth.

I would address this by implementing the following solutions:

1. **Decouple and Horizontally Scale Worker Nodes**:
   - Separate the API control plane from the worker execution fleet into independent microservices. Package the worker as an autonomous worker container deployed on Kubernetes or ECS and scale worker pods horizontally using a Horizontal Pod Autoscaler (HPA) driven by the Redis queue depth metric (`LLEN crawl_queue`).
2. **Distributed Queueing with Partitioned Task Queues**:
   - Transition from a single Redis list to a distributed broker (such as Redis Cluster, Celery with Celery Workers, or RabbitMQ with consistent hash exchanges) partitioning queues by domain or priority.
3. **Database Connection Pooling and Batch Ingestion**:
   - Deploy **PgBouncer** in front of PostgreSQL to pool thousands of client connections down to a fixed pool of server connections.
   - Replace single-row `INSERT` operations with micro-batched bulk inserts (`execute_values` or `COPY FROM STDIN`) buffering 100–500 page results before writing to disk.
   - Partition the `page_results` table by `job_id` or date ranges (declarative table partitioning) to maintain high insert throughput and fast query indexing.
4. **Distributed Redis Visited Sets & Bloom Filters**:
   - Offload URL deduplication from in-memory sets to Redis Sets with TTLs (`SADD` / `SISMEMBER` keyed by `job:visited:{job_id}`) or utilize **RedisBloom** (Scalable Bloom Filters) to achieve $O(1)$ deduplication checks with over 90% less memory.
5. **Distributed Proxy Rotation & Domain Politeness Rate Limiting**:
   - Implement an egress proxy rotator across diverse IP pools and enforce per-domain rate limiting (Token Bucket algorithm per domain in Redis) respecting `Retry-After` headers and `crawl-delay` directives.

---

### Question 3: Explain your strategy for handling failures during a crawl, such as network timeouts, invalid HTML, or a service crash. How does the system ensure job resilience?

**Response:**

For transient network errors and unexpected conditions during a crawl, I implemented a tiered, non-fatal containment strategy; for unrecoverable errors, the job is marked as `FAILED` with an informative message.

The system ensures resilience through the following strategies:

1. **Transient Network Errors & Timeouts**:
   - All HTTP GET requests enforce an explicit socket and connect timeout configured via `CRAWL_TIMEOUT_SECONDS` (default: 10s) to prevent threads from hanging indefinitely on slow or unresponsive servers.
   - When fetching discovered child links (pages with `depth > 0`), if a request encounters a timeout, connection reset, or HTTP error (e.g., 404 Not Found, 500 Internal Server Error), the exception is captured in a `try...except` block, logged with the active Thread ID, and `frontier.task_done()` is guaranteed to execute in the `finally` block. The worker thread continues processing remaining items in the frontier without aborting the overall job.

2. **Invalid or Malformed HTML**:
   - The crawler validates the `Content-Type` header and verifies the body starts with HTML markup; non-HTML assets (images, PDFs, binary streams) are safely bypassed.
   - HTML parsing uses `BeautifulSoup4` with Python's lenient `html.parser`, which gracefully constructs DOM trees even from malformed or unclosed tags.
   - If a page lacks a `<title>` tag or `<title>` is empty, the system defaults cleanly to an empty string `""` rather than throwing an exception.
   - Visible text extraction decomposes script, style, and SVG tags inside an error-handled block to avoid crashing word count calculations.

3. **Unrecoverable Errors & Seed Failures**:
   - If the `start_url` itself is invalid, unreachable, or fails DNS resolution (e.g. `NameResolutionError`, `ConnectionRefusedError`, or HTTP $\ge 400$), the crawler flags `start_url_failed = True` and captures the exact root cause.
   - Similarly, if `robots.txt` explicitly disallows crawling for user-agent `*` with `Disallow: /`, the crawl aborts before beginning.
   - When the frontier drains, if `start_url_failed` is true or 0 pages were successfully saved, the coordinator transactionally updates `crawl_jobs` status to `FAILED` and persists the `error_message` for API inspection.

4. **Service Crash Resilience**:
   - All job states (`PENDING`, `RUNNING`, `COMPLETED`, `FAILED`) are persisted transactionally in PostgreSQL with `created_at` and `updated_at` timestamps.
   - In production, a recovery daemon or watchdog can identify jobs that remain in `RUNNING` past a heartbeat deadline (e.g., jobs running longer than 1 hour without updates) and either transition them to `FAILED` or re-enqueue them for reprocessing.

---

### Question 4: Justify your database schema design. What trade-offs did you consider regarding normalization, indexing, and potential query performance for retrieving large result sets?

**Response:**

I chose a **normalized 3-tier relational schema** consisting of `users`, `crawl_jobs`, and `page_results` tables with explicit foreign key cascades.

1. **Normalization vs. Denormalization Trade-offs**:
   - **Normalization**: I kept `users`, `crawl_jobs`, and `page_results` normalized in third normal form (3NF). This prevents duplicating user credentials and job configurations across thousands of crawled page rows, saving massive storage space and preventing data anomalies.
   - **Selective Denormalization on `crawl_jobs`**: I chose to keep the execution telemetry fields (`depth`, `status`, `error_message`, and timestamps) directly on the `crawl_jobs` table. This allows status polling endpoints (`GET /api/jobs/{job_id}`) to read the complete job state in a single-row primary key lookup without needing to join with `page_results` or aggregate child rows.
   - **Lean Schema for `page_results`**: Rather than storing raw, multi-megabyte HTML response bodies in PostgreSQL (which would rapidly bloat table size and disk I/O), the table stores only the structured analytical artifacts required by the client contract: `url`, `title`, and `word_count` (integer). This design keeps row sizes compact (~150–250 bytes per page), enabling lightning-fast reads.

2. **Indexing Strategy**:
   - `users(email)`: B-Tree **UNIQUE** index for $O(1)$ credential verification during user authentication and immediate enforcement of duplicate registration conflicts (HTTP 409).
   - `crawl_jobs(user_id)`: B-Tree index on the foreign key to optimize user dashboard queries (`GET /api/jobs`) filtering jobs by `user_id`.
   - `crawl_jobs(status)`: B-Tree index supporting operational telemetry and worker queue monitoring.
   - `page_results(job_id)`: B-Tree index on `job_id`. Without this index, retrieving results for a completed job (`GET /api/jobs/{job_id}/results`) would require a full table scan across millions of crawled records. With this index, the query performs an index-range scan retrieving matching rows in milliseconds.

3. **Query Performance for Large Result Sets**:
   - The primary query `SELECT url, title, word_count FROM page_results WHERE job_id = %s ORDER BY id ASC` utilizes the index on `job_id`, streaming compact tuples directly to the client without loading extraneous columns.
   - For ultra-large crawls (100,000+ pages), the endpoint can readily adopt cursor-based pagination (`LIMIT` and `OFFSET` or keyset pagination `WHERE id > last_seen_id`) without requiring schema modifications.

---

### Question 5: Beyond JWT-based authentication, what are two other security risks for a public-facing web crawling service, and how would you mitigate them?

**Response:**

Beyond authentication, two critical security risks for a public-facing web crawling service are:

#### Risk 1: Server-Side Request Forgery (SSRF) and Cloud Metadata Exfiltration
- **The Risk**:
  - Malicious users can submit internal IP addresses, loopback addresses, or private cloud endpoints as the `start_url` (e.g., `http://169.254.169.254/latest/meta-data/` on AWS/GCP, `http://localhost:5432`, `http://10.0.0.1`, or `http://cache:6379`).
  - Because the crawler runs within the server's private network infrastructure, an unconstrained crawler would fetch internal data and return confidential infrastructure secrets, cloud credentials, or trigger internal administrative operations.
- **Mitigation Strategy**:
  1. **Strict Protocol Whitelisting**: Reject any protocol other than `http://` or `https://` (blocking `file://`, `gopher://`, `dict://`, `ftp://`).
  2. **Pre-Flight DNS Resolution & IP Range Validation**: Resolve target hostnames to IP addresses prior to initiating the HTTP connection. Block requests resolving to:
     - Loopback (`127.0.0.0/8`, `::1`)
     - RFC 1918 Private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)
     - Link-Local and Cloud Metadata endpoints (`169.254.0.0/16`)
     - Broadcast and Multicast ranges.
  3. **Egress Firewall Rules & Network Sandboxing**: Configure Docker network egress rules or iptables to forbid the crawler container from connecting to any internal service ports or the link-local metadata address.

#### Risk 2: Denial of Service via "Crawler Traps" (Decompression Bombs & Infinite Dynamic Paths)
- **The Risk**:
  - Adversaries can craft targeted crawler traps:
    - **Infinite Path Traps**: Dynamically generated links producing infinite directory recursion (e.g., `/items/1/items/2/items/3...` or infinite calendar dates).
    - **Decompression Bombs ("Zip/Gzip Bombs")**: Serving responses with `Content-Encoding: gzip` containing a tiny 10KB compressed payload that decompresses into 50GB of null bytes in memory.
  - This exhausts server RAM, fills database disk storage, saturates thread pool capacity, and locks up background workers.
- **Mitigation Strategy**:
  1. **Hard Upper Bounds on Crawl Depth and Page Volume**: Enforce a global ceiling on crawl depth (e.g. maximum depth 5) and a hard cap on the maximum number of pages allowed per crawl job (e.g., stopping the job after 500 pages).
  2. **Streamed Content-Length & Response Size Limits**: Use streamed HTTP requests (`requests.get(..., stream=True)`), checking the `Content-Length` header upfront and aborting if it exceeds a threshold (e.g. 5 MB). Additionally, count chunk sizes while reading and terminate downloads immediately if decompressed data exceeds the limit.
  3. **Job Execution Timeout Guard**: Enforce a global wall-clock deadline on the crawl coordinator (e.g., maximum 5 minutes per job) to kill runaway crawl executions regardless of frontier state.
