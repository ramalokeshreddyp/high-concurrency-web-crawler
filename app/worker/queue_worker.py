import json
import logging
import threading
import time
import redis

from app.config import config
from app.worker.crawler import JobCrawler, update_job_status

logger = logging.getLogger(__name__)

class QueueWorkerManager:
    """
    Background listener for jobs on Redis queue.
    Dequeues jobs using BRPOP and triggers JobCrawler.
    """
    def __init__(self):
        self._running = False
        self._thread: threading.Thread = None
        self._redis_client: redis.Redis = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="RedisQueueListener", daemon=True)
        self._thread.start()
        logger.info(f"QueueWorkerManager started, listening on Redis queue '{config.REDIS_QUEUE_NAME}'...")

    def stop(self):
        logger.info("Stopping QueueWorkerManager...")
        self._running = False
        if self._thread:
            self._thread.join(timeout=3.0)
        logger.info("QueueWorkerManager stopped.")

    def _get_redis(self) -> redis.Redis:
        if self._redis_client is None:
            self._redis_client = redis.Redis(
                host=config.REDIS_HOST,
                port=config.REDIS_PORT,
                decode_responses=True
            )
        return self._redis_client

    def _run_loop(self):
        while self._running:
            try:
                r = self._get_redis()
                # BRPOP with a short timeout allows checking self._running periodically
                item = r.brpop(config.REDIS_QUEUE_NAME, timeout=2)
                if not item:
                    continue

                queue_name, raw_data = item
                logger.info(f"Dequeued job from {queue_name}: {raw_data}")

                try:
                    payload = json.loads(raw_data)
                    job_id = payload["job_id"]
                    start_url = payload["start_url"]
                    depth = int(payload.get("depth", 0))

                    crawler = JobCrawler(
                        job_id=job_id,
                        start_url=start_url,
                        max_depth=depth,
                        num_threads=config.WORKER_THREADS
                    )
                    crawler.run()

                except Exception as ex:
                    logger.exception(f"Unhandled error processing job message {raw_data}: {ex}")
                    try:
                        if "job_id" in locals():
                            update_job_status(job_id, "FAILED", error_message=f"Worker error: {str(ex)}")
                    except Exception:
                        pass

            except redis.ConnectionError as ce:
                logger.warning(f"Redis connection error in worker loop: {ce}. Retrying in 2s...")
                time.sleep(2)
            except Exception as e:
                logger.error(f"Unexpected error in worker loop: {e}")
                time.sleep(1)

queue_worker = QueueWorkerManager()
