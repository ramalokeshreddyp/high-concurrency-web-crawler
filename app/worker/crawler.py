import logging
import queue
import threading
import urllib.parse
from typing import Set, Tuple, Optional
import requests
from bs4 import BeautifulSoup

from app.config import config
from app.database import get_db_cursor

logger = logging.getLogger(__name__)

def check_robots_txt(start_url: str, timeout: int = 5) -> Tuple[bool, Optional[str]]:
    """
    Checks if start_url/robots.txt disallows all crawling.
    Returns (allowed: bool, reason: Optional[str]).
    """
    try:
        parsed = urllib.parse.urlparse(start_url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        resp = requests.get(
            robots_url,
            timeout=timeout,
            headers={"User-Agent": "HighConcurrencyWebCrawler/1.0"}
        )
        if resp.status_code == 200:
            lines = [l.strip().lower() for l in resp.text.splitlines() if l.strip() and not l.strip().startswith("#")]
            ua_match = False
            for line in lines:
                if line.startswith("user-agent:"):
                    ua = line.split(":", 1)[1].strip()
                    ua_match = (ua == "*")
                elif ua_match and line.startswith("disallow:"):
                    disallow_path = line.split(":", 1)[1].strip()
                    if disallow_path == "/":
                        return False, "Disallowed by robots.txt (Disallow: /)"
    except Exception as e:
        logger.debug(f"Could not check robots.txt for {start_url} ({e}). Proceeding with crawl.")
    return True, None

def normalize_url(raw_url: str, base_url: str) -> Optional[str]:
    """Resolves relative URLs, removes fragments, and normalizes schemes."""
    try:
        full_url = urllib.parse.urljoin(base_url, raw_url.strip())
        parsed = urllib.parse.urlparse(full_url)
        if parsed.scheme not in ("http", "https"):
            return None
        # Drop fragment and normalize netloc to lowercase
        normalized = urllib.parse.urlunparse((
            parsed.scheme,
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.params,
            parsed.query,
            ""  # strip fragment
        ))
        return normalized
    except Exception:
        return None

def extract_domain(url: str) -> str:
    """Extracts base host domain without port or credentials."""
    netloc = urllib.parse.urlparse(url).netloc.lower()
    return netloc.split(":")[0]

def save_page_result(job_id: int, url: str, title: str, word_count: int):
    """Saves a single crawled page result into the database using a thread-safe connection."""
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            INSERT INTO page_results (job_id, url, title, word_count)
            VALUES (%s, %s, %s, %s)
            """,
            (job_id, url, title, word_count)
        )

def update_job_status(job_id: int, status: str, error_message: Optional[str] = None):
    """Updates job status and optional error message."""
    with get_db_cursor(commit=True) as cur:
        cur.execute(
            """
            UPDATE crawl_jobs
            SET status = %s, error_message = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (status, error_message, job_id)
        )

class JobCrawler:
    """
    Manages multithreaded crawling for a single crawl job.
    Uses WORKER_THREADS worker threads, thread-safe frontier queue, and visited set.
    """
    def __init__(self, job_id: int, start_url: str, max_depth: int, num_threads: int = None):
        self.job_id = job_id
        self.start_url = start_url
        self.max_depth = max_depth
        self.num_threads = num_threads or config.WORKER_THREADS

        self.frontier = queue.Queue()
        self.visited: Set[str] = set()
        self.visited_lock = threading.Lock()

        self.results_count = 0
        self.results_lock = threading.Lock()

        self.start_url_failed = False
        self.failure_reason: Optional[str] = None
        self.failure_lock = threading.Lock()

        self.base_domain = extract_domain(start_url)
        self.stop_event = threading.Event()

    def run(self):
        logger.info(f"Starting crawl for Job {self.job_id} on {self.start_url} with depth={self.max_depth}, threads={self.num_threads}")
        update_job_status(self.job_id, "RUNNING")

        # 1. Robots.txt check
        allowed, reason = check_robots_txt(self.start_url)
        if not allowed:
            logger.warning(f"Job {self.job_id} rejected by robots.txt: {reason}")
            update_job_status(self.job_id, "FAILED", error_message=reason)
            return

        # 2. Seed the frontier
        normalized_seed = normalize_url(self.start_url, self.start_url)
        if not normalized_seed:
            update_job_status(self.job_id, "FAILED", error_message="Invalid start_url format")
            return

        self.visited.add(normalized_seed)
        self.frontier.put((normalized_seed, 0))

        # 3. Spawn worker threads
        threads = []
        for i in range(self.num_threads):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"CrawlWorker-{self.job_id}-{i}",
                daemon=True
            )
            t.start()
            threads.append(t)

        # 4. Wait for frontier to drain completely
        self.frontier.join()

        # 5. Signal threads to stop and join
        self.stop_event.set()
        for _ in range(self.num_threads):
            self.frontier.put(None)

        for t in threads:
            t.join(timeout=2.0)

        # 6. Evaluate job outcome
        if self.start_url_failed or self.results_count == 0:
            err = self.failure_reason or "Failed to crawl start_url"
            logger.error(f"Job {self.job_id} FAILED: {err}")
            update_job_status(self.job_id, "FAILED", error_message=err)
        else:
            logger.info(f"Job {self.job_id} COMPLETED with {self.results_count} pages crawled.")
            update_job_status(self.job_id, "COMPLETED")

    def _worker_loop(self):
        thread_id = threading.get_ident()
        thread_name = threading.current_thread().name

        while not self.stop_event.is_set():
            try:
                item = self.frontier.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is None:
                self.frontier.task_done()
                break

            url, cur_depth = item

            try:
                logger.info(f"[{thread_name} (TID {thread_id})] Fetching: {url} (depth={cur_depth}/{self.max_depth})")
                resp = requests.get(
                    url,
                    timeout=config.CRAWL_TIMEOUT_SECONDS,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; HighConcurrencyWebCrawler/1.0)"},
                    allow_redirects=True
                )

                if resp.status_code >= 400:
                    logger.warning(f"[{thread_name}] HTTP {resp.status_code} for {url}")
                    if cur_depth == 0:
                        with self.failure_lock:
                            self.start_url_failed = True
                            self.failure_reason = f"HTTP {resp.status_code} error on start_url"
                    continue

                # Parse HTML
                content_type = resp.headers.get("Content-Type", "")
                if "html" not in content_type.lower() and not resp.text.strip().startswith("<"):
                    logger.info(f"[{thread_name}] Skipping non-HTML resource: {url} ({content_type})")
                    continue

                soup = BeautifulSoup(resp.content, "html.parser")

                # Extract title
                title = ""
                if soup.title and soup.title.string:
                    title = soup.title.string.strip()
                elif soup.title:
                    title = soup.title.get_text(strip=True)

                # Extract visible text for word count
                for tag in soup(["script", "style", "noscript", "svg"]):
                    tag.decompose()
                text = soup.get_text(separator=" ", strip=True)
                word_count = len(text.split()) if text else 0

                # Persist result to database
                save_page_result(self.job_id, url, title, word_count)
                with self.results_lock:
                    self.results_count += 1

                # Discovered links processing if current depth < max_depth
                if cur_depth < self.max_depth and not self.stop_event.is_set():
                    links_to_add = []
                    for a_tag in soup.find_all("a", href=True):
                        href = a_tag["href"].strip()
                        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
                            continue

                        norm_link = normalize_url(href, url)
                        if not norm_link:
                            continue

                        # Check same-domain constraint
                        link_domain = extract_domain(norm_link)
                        if link_domain == self.base_domain:
                            with self.visited_lock:
                                if norm_link not in self.visited:
                                    self.visited.add(norm_link)
                                    links_to_add.append(norm_link)

                    for new_link in links_to_add:
                        logger.info(f"[{thread_name}] Discovered new link: {new_link} -> Queueing at depth {cur_depth + 1}")
                        self.frontier.put((new_link, cur_depth + 1))

            except Exception as e:
                logger.error(f"[{thread_name}] Error crawling {url}: {e}")
                if cur_depth == 0:
                    with self.failure_lock:
                        self.start_url_failed = True
                        self.failure_reason = f"Network or parsing error: {str(e)}"
            finally:
                self.frontier.task_done()
