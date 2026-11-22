"""Trend-fact contract between the ai-knowledge-service (producer) and trends-service.

One fact per reviewed (APPROVED or CORRECTED) RCA, built from the job payload only, so
the trends-service never reads another service's collections.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

TREND_FACT_JOB = "trend_fact"
UNKNOWN = "Unknown"

# Public group-by / filter names -> trend_facts fields.
DIMENSIONS: dict[str, str] = {
    "module": "module",
    "release": "release",
    "month": "month",
    "root_cause": "root_cause_code",
    "sdlc_phase": "sdlc_phase_code",
    "testing_gap": "testing_gap_code",
    "severity": "severity",
}


class TrendFact(BaseModel):
    project_id: str = Field(min_length=2, max_length=63)
    defect_id: int = Field(ge=1)
    rca_id: str = Field(min_length=1, max_length=64)
    status: Literal["APPROVED", "CORRECTED"]
    module: str = Field(default=UNKNOWN, max_length=500)
    release: str = Field(default=UNKNOWN, max_length=500)
    month: str = Field(default=UNKNOWN, max_length=7)
    severity: str = Field(default=UNKNOWN, max_length=100)
    root_cause_code: str = Field(max_length=64)
    sdlc_phase_code: str = Field(max_length=64)
    testing_gap_code: str = Field(max_length=64)
    ai_root_cause_code: str | None = Field(default=None, max_length=64)
    reviewed_at: datetime | None = None
