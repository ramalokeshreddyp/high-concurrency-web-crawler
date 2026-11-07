from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, model_validator

class JobCreateRequest(BaseModel):
    start_url: str = Field(..., description="The seed URL to start crawling from")
    depth: Optional[int] = Field(default=0, ge=0, description="The crawl depth")
    crawl_depth: Optional[int] = Field(default=None, ge=0, description="Alternative field for depth")

    @model_validator(mode="before")
    @classmethod
    def resolve_depth(cls, values):
        if isinstance(values, dict):
            if "crawl_depth" in values and "depth" not in values:
                values["depth"] = values["crawl_depth"]
        return values

class JobCreateResponse(BaseModel):
    job_id: int
    status: str = "PENDING"
    message: Optional[str] = "Job accepted for processing"

class JobResponse(BaseModel):
    id: int
    start_url: str
    depth: int
    status: str
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

class PageResultResponse(BaseModel):
    url: str
    title: Optional[str] = ""
    word_count: int = 0
