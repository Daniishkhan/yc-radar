from __future__ import annotations

import json

from yc_radar.agents.llm import LLMClient, get_llm_client
from yc_radar.domain.models import Company, OutreachBrief, PrototypeMission
from yc_radar.playbooks.engine import outreach_for


class ScoutAgent:
    """Refines deterministic missions into founder-ready briefs when an LLM is available."""

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or get_llm_client()

    async def refine_outreach(self, company: Company, mission: PrototypeMission) -> OutreachBrief:
        base = outreach_for(company, mission)
        system = (
            "You are helping a senior backend/software engineer get noticed by a small company. "
            "Use AI and data systems experience as supporting proof, not as the primary role lane. "
            "Write concise, specific outreach. Do not sound like a recruiter or a mass email. "
            "Never claim the prototype is complete unless the user provides repo and demo URLs."
        )
        user = json.dumps(
            {
                "company": company.model_dump(),
                "mission": mission.model_dump(),
                "base_subject": base.subject,
                "base_body": base.body,
                "instructions": (
                    "Return JSON with subject and body. Keep body under 170 words. "
                    "Use placeholders {founder_first_name}, {repo_url}, and {loom_url}."
                ),
            },
            indent=2,
        )
        raw = await self.llm.complete(system=system, user=user)
        parsed = json.loads(raw)
        return OutreachBrief(
            company=company,
            subject=parsed["subject"],
            body=parsed["body"],
            mission=mission,
            llm_used=True,
        )
