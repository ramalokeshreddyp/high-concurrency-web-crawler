import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import config
from app.database import init_db
from app.auth.routes import router as auth_router
from app.jobs.routes import router as jobs_router
from app.worker.queue_worker import queue_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("crawler_api")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    init_db()
    logger.info("Starting background Redis queue worker manager...")
    queue_worker.start()
    yield
    logger.info("Shutting down background workers...")
    queue_worker.stop()
    logger.info("Application shutdown complete.")

app = FastAPI(
    title="High-Concurrency Web Crawler API",
    description="Multi-service web crawler and content analysis service with JWT authentication.",
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Healthcheck endpoints
@app.get("/health", tags=["Health"])
@app.get("/api/health", tags=["Health"])
def health_check():
    return {"status": "healthy"}

@app.get("/", tags=["Root"])
def root():
    return {
        "service": "High-Concurrency Web Crawler Service",
        "status": "online",
        "documentation": "/docs"
    }

# Register routers
app.include_router(auth_router)
app.include_router(jobs_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)
