from __future__ import annotations

from pydantic import BaseModel, Field


class Company(BaseModel):
    id: int | None = None
    name: str
    slug: str
    yc_url: str | None = None
    website: str | None = None
    one_liner: str | None = None
    batch: str | None = None
    status: str | None = None
    stage: str | None = None
    team_size: int | None = None
    is_hiring: bool = Field(default=False, alias="isHiring")
    all_locations: str | None = None
    regions: list[str] = Field(default_factory=list)
    industry: str | None = None
    subindustry: str | None = None
    industries: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    prototype_score: int | None = None
    prototype_angle: str | None = None

    model_config = {"populate_by_name": True}
