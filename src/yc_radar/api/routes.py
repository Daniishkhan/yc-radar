from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from yc_radar.agents.scout import ScoutAgent
from yc_radar.domain.models import CompanyList, OutreachBrief, PrototypeMission
from yc_radar.playbooks.engine import mission_for, outreach_for
from yc_radar.services.company_repository import CompanyRepository, get_company_repository


router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/companies", response_model=CompanyList)
def list_companies(
    query: str | None = None,
    hiring: bool | None = None,
    remote: bool | None = None,
    max_team_size: int | None = None,
    industries: list[str] | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    repo: CompanyRepository = Depends(get_company_repository),
) -> CompanyList:
    companies = repo.search(
        query=query,
        hiring=hiring,
        remote=remote,
        max_team_size=max_team_size,
        industries=industries,
    )
    return CompanyList(
        total=len(companies),
        limit=limit,
        offset=offset,
        companies=companies[offset : offset + limit],
    )


@router.get("/companies/{slug}")
def get_company(
    slug: str,
    repo: CompanyRepository = Depends(get_company_repository),
):
    company = repo.get_by_slug(slug)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.get("/targets", response_model=CompanyList)
def list_targets(
    query: str | None = None,
    hiring: bool = True,
    remote: bool | None = None,
    max_team_size: int | None = 10,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    repo: CompanyRepository = Depends(get_company_repository),
) -> CompanyList:
    companies = repo.search(
        query=query,
        hiring=hiring,
        remote=remote,
        max_team_size=max_team_size,
    )
    return CompanyList(
        total=len(companies),
        limit=limit,
        offset=offset,
        companies=companies[offset : offset + limit],
    )


@router.get("/missions/{slug}", response_model=PrototypeMission)
def get_mission(
    slug: str,
    repo: CompanyRepository = Depends(get_company_repository),
) -> PrototypeMission:
    company = repo.get_by_slug(slug)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return mission_for(company)


@router.post("/missions/{slug}/brief", response_model=OutreachBrief)
async def create_outreach_brief(
    slug: str,
    use_llm: bool = False,
    repo: CompanyRepository = Depends(get_company_repository),
) -> OutreachBrief:
    company = repo.get_by_slug(slug)
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")

    mission = mission_for(company)
    if not use_llm:
        return outreach_for(company, mission)

    try:
        return await ScoutAgent().refine_outreach(company, mission)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"LLM outreach unavailable: {exc}") from exc
