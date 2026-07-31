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

    @property
    def is_remote_friendly(self) -> bool:
        return any("Remote" in region for region in self.regions)

    @property
    def text_blob(self) -> str:
        parts = [
            self.name,
            self.one_liner,
            self.industry,
            self.subindustry,
            " ".join(self.industries),
            " ".join(self.tags),
            " ".join(self.regions),
        ]
        return " ".join(part for part in parts if part).lower()


class PrototypeMission(BaseModel):
    company: Company
    playbook: str
    score: int
    thesis: str
    artifact: str
    build_steps: list[str]
    proof_points: list[str]
    outreach_angle: str
    risks: list[str] = Field(default_factory=list)


class OutreachBrief(BaseModel):
    company: Company
    subject: str
    body: str
    mission: PrototypeMission
    llm_used: bool = False
