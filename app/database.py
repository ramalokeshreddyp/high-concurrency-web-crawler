import logging
import time
from contextlib import contextmanager
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from app.config import config

logger = logging.getLogger(__name__)

db_pool: pool.ThreadedConnectionPool = None

def get_db_pool() -> pool.ThreadedConnectionPool:
    global db_pool
    if db_pool is None:
        retries = 10
        delay = 2
        for attempt in range(retries):
            try:
                db_pool = pool.ThreadedConnectionPool(
                    minconn=2,
                    maxconn=max(10, config.WORKER_THREADS * 3),
                    host=config.POSTGRES_HOST,
                    port=config.POSTGRES_PORT,
                    user=config.POSTGRES_USER,
                    password=config.POSTGRES_PASSWORD,
                    dbname=config.POSTGRES_DB
                )
                logger.info("PostgreSQL ThreadedConnectionPool successfully initialized.")
                break
            except Exception as e:
                logger.warning(f"Database connection attempt {attempt + 1}/{retries} failed: {e}. Retrying in {delay}s...")
                time.sleep(delay)
        if db_pool is None:
            raise RuntimeError("Could not connect to PostgreSQL after several retries.")
    return db_pool

@contextmanager
def get_db():
    """Context manager for thread-safe database connection retrieval."""
    p = get_db_pool()
    conn = p.getconn()
    try:
        yield conn
    finally:
        p.putconn(conn)

@contextmanager
def get_db_cursor(commit=False):
    """Context manager for obtaining a database cursor with automatic rollback/commit."""
    with get_db() as conn:
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cursor
            if commit:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()

def init_db():
    """Ensure required tables exist and test user is seeded."""
    with get_db_cursor(commit=True) as cur:
        # Users table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Crawl jobs table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                start_url TEXT NOT NULL,
                depth INTEGER NOT NULL DEFAULT 0,
                status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
                error_message TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Page results table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS page_results (
                id SERIAL PRIMARY KEY,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                url TEXT NOT NULL,
                title TEXT,
                word_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Indices
        cur.execute("CREATE INDEX IF NOT EXISTS idx_crawl_jobs_user_id ON crawl_jobs(user_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_crawl_jobs_status ON crawl_jobs(status);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_page_results_job_id ON page_results(job_id);")

        # Seed test user from submission.json
        cur.execute("""
            INSERT INTO users (email, password_hash)
            VALUES ('user@example.com', '$2b$12$LsKYmc4hepR46RN2XhIyP.AvLkIzakr9GTSZo.SIKrVFo8hkeOLcC')
            ON CONFLICT (email) DO NOTHING;
        """)
        logger.info("Database tables and seed user verified.")
