from __future__ import annotations

from yc_radar.domain.models import Company, OutreachBrief, PrototypeMission


def select_playbook(company: Company) -> str:
    blob = company.text_blob
    if "open source" in blob or "developer tools" in blob or "infrastructure" in blob:
        return "open_source_pr_or_sdk_demo"
    if "agent" in blob or "ai" in blob or "artificial intelligence" in blob:
        return "ai_workflow_prototype"
    if "security" in blob or "compliance" in blob:
        return "audit_workflow_demo"
    if "fintech" in blob or "payments" in blob or "finance" in blob:
        return "ops_dashboard_demo"
    if "healthcare" in blob:
        return "synthetic_ops_demo"
    if "recruiting" in blob or "human resources" in blob:
        return "matching_or_automation_demo"
    return "customer_workflow_demo"


def mission_for(company: Company) -> PrototypeMission:
    playbook = select_playbook(company)
    builders = {
        "open_source_pr_or_sdk_demo": _open_source_mission,
        "ai_workflow_prototype": _ai_workflow_mission,
        "audit_workflow_demo": _audit_mission,
        "ops_dashboard_demo": _ops_dashboard_mission,
        "synthetic_ops_demo": _synthetic_ops_mission,
        "matching_or_automation_demo": _matching_mission,
        "customer_workflow_demo": _customer_workflow_mission,
    }
    return builders[playbook](company)


def outreach_for(company: Company, mission: PrototypeMission) -> OutreachBrief:
    subject = f"Built a small {company.name} prototype"
    body = (
        f"Hi {{founder_first_name}},\n\n"
        f"I came across {company.name} and liked the wedge: "
        f"{company.one_liner or 'the product direction on your YC profile'}.\n\n"
        f"I built a small prototype around one workflow I think could matter: "
        f"{mission.artifact}. The goal was to make the value obvious in under a minute, "
        f"not to pitch a generic application.\n\n"
        f"Repo: {{repo_url}}\n"
        f"Loom: {{loom_url}}\n\n"
        f"If this is useful, I would love to show how I would take it from demo to production. "
        f"I am looking for a senior AI/backend/product-engineering role where I can ship this kind "
        f"of thing quickly.\n\n"
        f"Best,\nDanish"
    )
    return OutreachBrief(company=company, subject=subject, body=body, mission=mission)


def _open_source_mission(company: Company) -> PrototypeMission:
    return PrototypeMission(
        company=company,
        playbook="open_source_pr_or_sdk_demo",
        score=company.prototype_score or 0,
        thesis=(
            f"{company.name} looks like a product where a high-quality integration, SDK example, "
            "or docs-backed PR can prove engineering judgment quickly."
        ),
        artifact="a small PR or companion example app that removes one adoption blocker",
        build_steps=[
            "Find the public repo, docs, examples, and most recent release notes.",
            "Identify one missing integration, broken quickstart, or under-documented workflow.",
            "Ship a narrow PR or example app with tests and clear docs.",
            "Record a 60-second Loom showing before, after, and why it matters.",
        ],
        proof_points=[
            "Readable production-style code",
            "Tests or a reproducible demo command",
            "A changelog-quality explanation of the user impact",
        ],
        outreach_angle="I found one adoption blocker and shipped a working improvement.",
        risks=["Avoid giant refactors; founders notice useful, merged-sized work."],
    )


def _ai_workflow_mission(company: Company) -> PrototypeMission:
    return PrototypeMission(
        company=company,
        playbook="ai_workflow_prototype",
        score=company.prototype_score or 0,
        thesis=(
            f"{company.name} is AI-adjacent enough that an end-to-end agent workflow can show "
            "taste, systems thinking, and speed."
        ),
        artifact="an agent that automates one painful product or customer workflow end to end",
        build_steps=[
            "Extract the core user workflow from their homepage, docs, and YC profile.",
            "Create a synthetic but realistic dataset for that workflow.",
            "Build an agent with tool calls, trace logs, eval cases, and failure states.",
            "Package it as a hosted demo or local Docker command.",
        ],
        proof_points=[
            "Agent does real work instead of just chatting",
            "Observable traces and evals",
            "Clear constraints, retries, and human handoff",
        ],
        outreach_angle="I built a small agent that demonstrates how your product could own this workflow.",
    )


def _audit_mission(company: Company) -> PrototypeMission:
    return PrototypeMission(
        company=company,
        playbook="audit_workflow_demo",
        score=company.prototype_score or 0,
        thesis="Security and compliance buyers respond to evidence, logs, and repeatable workflows.",
        artifact="an audit-trail workflow with screenshots, event logs, and a downloadable report",
        build_steps=[
            "Model a realistic admin/security workflow.",
            "Automate the workflow with a browser or API tool.",
            "Capture actions, timestamps, before/after state, and screenshots.",
            "Generate a compact report a buyer could forward internally.",
        ],
        proof_points=["Audit log", "Screenshots", "Exportable JSON/PDF", "Failure-mode handling"],
        outreach_angle="I built an evidence-first workflow that could help sell trust faster.",
    )


def _ops_dashboard_mission(company: Company) -> PrototypeMission:
    return PrototypeMission(
        company=company,
        playbook="ops_dashboard_demo",
        score=company.prototype_score or 0,
        thesis="Finance and ops products benefit from demos that turn messy records into decisions.",
        artifact="a reconciliation or risk dashboard with explainable AI-generated flags",
        build_steps=[
            "Create synthetic transactions, invoices, payouts, or account records.",
            "Build ingestion and normalization.",
            "Add explainable flags with citations to source rows.",
            "Show the operator workflow: review, override, export.",
        ],
        proof_points=["Data model", "Explainability", "Operator UX", "CSV/API export"],
        outreach_angle="I built a buyer-facing workflow that makes the operational ROI visible.",
    )


def _synthetic_ops_mission(company: Company) -> PrototypeMission:
    return PrototypeMission(
        company=company,
        playbook="synthetic_ops_demo",
        score=company.prototype_score or 0,
        thesis="Healthcare prototypes need to show workflow empathy without touching real patient data.",
        artifact="a synthetic intake, triage, or provider-ops workflow with safe test data",
        build_steps=[
            "Create synthetic healthcare records and constraints.",
            "Build a narrow workflow around intake, triage, follow-up, or admin ops.",
            "Add guardrails for uncertainty and human review.",
            "Show measurable time saved without overclaiming clinical accuracy.",
        ],
        proof_points=["Synthetic data", "Guardrails", "Human review path", "Operational metric"],
        outreach_angle="I built a safe workflow demo around the operational problem, not medical claims.",
    )


def _matching_mission(company: Company) -> PrototypeMission:
    return PrototypeMission(
        company=company,
        playbook="matching_or_automation_demo",
        score=company.prototype_score or 0,
        thesis="Talent products need trustable ranking, transparent scoring, and workflow automation.",
        artifact="a matching or candidate-ops workflow with transparent scoring and review queues",
        build_steps=[
            "Create realistic candidate/job or employee/access data.",
            "Build scoring with explainable factors and red flags.",
            "Add review queues and decision logging.",
            "Export a summary a human operator can act on.",
        ],
        proof_points=["Transparent score", "Review queue", "Decision log", "Operator export"],
        outreach_angle="I built a workflow that keeps humans in control while AI removes sorting work.",
    )


def _customer_workflow_mission(company: Company) -> PrototypeMission:
    return PrototypeMission(
        company=company,
        playbook="customer_workflow_demo",
        score=company.prototype_score or 0,
        thesis=(
            f"{company.name} has a clear enough wedge to build a narrow customer workflow "
            "that makes the value concrete."
        ),
        artifact="a mini customer demo that turns their one-liner into a working workflow",
        build_steps=[
            "Turn the YC one-liner into a concrete user story.",
            "Create a tiny realistic dataset.",
            "Build the smallest useful workflow with one clear before/after moment.",
            "Ship a repo, screenshots, and a Loom.",
        ],
        proof_points=["Specific user story", "Working demo", "Before/after comparison", "Short Loom"],
        outreach_angle="I converted your positioning into a working demo and would love to compare notes.",
    )

