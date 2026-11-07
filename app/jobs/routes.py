import json
import logging
from typing import List
from fastapi import APIRouter, HTTPException, status, Depends
import redis

from app.config import config
from app.database import get_db_cursor
from app.auth.routes import get_current_user
from app.jobs.models import (
    JobCreateRequest,
    JobCreateResponse,
    JobResponse,
    PageResultResponse
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["Crawl Jobs"])

_redis_client = None

def get_redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.Redis(
            host=config.REDIS_HOST,
            port=config.REDIS_PORT,
            decode_responses=True
        )
    return _redis_client

@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=JobCreateResponse)
@router.post("/", status_code=status.HTTP_202_ACCEPTED, response_model=JobCreateResponse)
def submit_job(req: JobCreateRequest, current_user: dict = Depends(get_current_user)):
    url = req.start_url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_url must begin with http:// or https://"
        )

    depth = req.depth if req.depth is not None else 0

    try:
        with get_db_cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO crawl_jobs (user_id, start_url, depth, status)
                VALUES (%s, %s, %s, 'PENDING')
                RETURNING id, start_url, depth, status, created_at
                """,
                (current_user["id"], url, depth)
            )
            job = cur.fetchone()

        job_id = job["id"]

        # Push to Redis queue
        r = get_redis_client()
        job_payload = {
            "job_id": job_id,
            "user_id": current_user["id"],
            "start_url": url,
            "depth": depth
        }
        r.lpush(config.REDIS_QUEUE_NAME, json.dumps(job_payload))
        logger.info(f"Job {job_id} submitted by user {current_user['id']} and pushed to queue '{config.REDIS_QUEUE_NAME}'.")

        return {
            "job_id": job_id,
            "status": "PENDING",
            "message": "Job accepted for processing"
        }
    except Exception as e:
        logger.error(f"Failed to submit job: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to submit crawl job: {str(e)}"
        )

@router.get("", response_model=List[JobResponse])
@router.get("/", response_model=List[JobResponse])
def list_jobs(current_user: dict = Depends(get_current_user)):
    with get_db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, start_url, depth, status, error_message, created_at, updated_at
            FROM crawl_jobs
            WHERE user_id = %s
            ORDER BY created_at DESC
            """,
            (current_user["id"],)
        )
        jobs = cur.fetchall()
        return jobs

@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: int, current_user: dict = Depends(get_current_user)):
    with get_db_cursor(commit=False) as cur:
        cur.execute(
            """
            SELECT id, user_id, start_url, depth, status, error_message, created_at, updated_at
            FROM crawl_jobs
            WHERE id = %s
            """,
            (job_id,)
        )
        job = cur.fetchone()

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found"
        )

    if job["user_id"] != current_user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this job"
        )

    return job

@router.get("/{job_id}/results", response_model=List[PageResultResponse])
def get_job_results(job_id: int, current_user: dict = Depends(get_current_user)):
    with get_db_cursor(commit=False) as cur:
        # Check job existence and user ownership
        cur.execute("SELECT id, user_id, status FROM crawl_jobs WHERE id = %s", (job_id,))
        job = cur.fetchone()

        if not job:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Job not found"
            )

        if job["user_id"] != current_user["id"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to view results for this job"
            )

        cur.execute(
            """
            SELECT url, title, word_count
            FROM page_results
            WHERE job_id = %s
            ORDER BY id ASC
            """,
            (job_id,)
        )
        results = cur.fetchall()
        return results
