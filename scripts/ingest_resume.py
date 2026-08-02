#!/usr/bin/env python3
"""Extract Danish's resume into agent-friendly structured JSON."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from yc_radar.core.config import get_settings


def normalize_text(text: str) -> str:
    replacements = {
        "D anish": "Danish",
        "So ftw ar e": "Software",
        "F ull S tack": "Full Stack",
        "T ype Scrip t": "TypeScript",
        "N ode": "Node",
        "F astapi": "FastAPI",
        "R eact": "React",
        "N e xt.js": "Next.js",
        "D ata": "Data",
        "P akistan": "Pakistan",
        "Link edin": "LinkedIn",
        "danisha fzalkhan@gmail.c om": "danishafzalkhan@gmail.com",
        "092 - 3369009019": "+92 3369009019",
    }
    cleaned = text
    for source, target in replacements.items():
        cleaned = cleaned.replace(source, target)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def extract_pdf_text(path: Path) -> tuple[str, int]:
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return normalize_text("\n\n".join(pages)), len(reader.pages)


def build_profile(resume_path: Path, raw_text: str, pages: int) -> dict[str, Any]:
    return {
        "name": "Danish Khan",
        "headline": "Senior Software / Full Stack / Backend / Frontend / AI Engineer",
        "contact": {
            "email": "danishafzalkhan@gmail.com",
            "phone": "+92 3369009019",
            "location": "Pakistan",
            "linkedin": "https://www.linkedin.com/in/danishafzalkhan",
        },
        "summary": (
            "Senior backend/software engineer with 6+ years of experience building backend "
            "systems, full-stack applications, data pipelines, and LLM-powered products. "
            "Recent work includes autonomous coding harnesses, local inference infrastructure, "
            "LLM evaluation benchmarks, high-volume ETL, and integrations for downstream model "
            "quality."
        ),
        "target_roles": [
            "Senior Backend Engineer",
            "Senior Software Engineer",
            "Backend Platform Engineer",
            "Infrastructure Engineer",
            "Backend-heavy Founding Engineer",
            "Backend-heavy Full Stack Engineer",
            "Senior Full Stack Engineer",
            "Senior Frontend Engineer",
            "AI Engineer",
            "Applied AI Engineer",
        ],
        "supporting_strengths": [
            "AI engineering",
            "Large language models",
            "Data engineering",
            "Full-stack product delivery",
        ],
        "core_expertise": [
            "Large language models",
            "AI engineering",
            "Backend systems",
            "Data engineering",
            "Full-stack product engineering",
            "Distributed systems",
            "ETL and high-volume ingestion",
            "Evaluation benchmarks",
            "Agentic coding workflows",
        ],
        "technical_skills": {
            "languages": ["Python", "TypeScript", "JavaScript"],
            "backend": ["Node.js", "NestJS", "FastAPI", "REST APIs", "event-driven architecture"],
            "frontend": ["React", "Next.js"],
            "ai_llm": [
                "OpenAI",
                "Azure OpenAI",
                "LangChain",
                "open-source model fine-tuning",
                "LLM evaluation benchmarks",
                "local inference infrastructure",
                "batch inference",
                "autonomous coding agents",
            ],
            "data": [
                "ETL pipelines",
                "data warehouses",
                "large-scale data ingestion",
                "data quality integrations",
                "training data pipelines",
            ],
            "cloud_infra": ["AWS", "Azure", "GCP", "Docker", "Kubernetes"],
            "product": [
                "technical product execution",
                "customer-driven configuration",
                "founding engineer workflows",
                "mentoring junior engineers",
            ],
        },
        "experience": [
            {
                "title": "Senior Full Stack Engineer",
                "company": "Tough Leaf",
                "start_date": "August 2024",
                "end_date": "March 2026",
                "location": "USA (Remote)",
                "highlights": [
                    "Led the data engineering team with a focus on ETL pipelines for high-volume data ingestion.",
                    "Shipped full-stack product features from specifications to launch with the platform team.",
                    "Ran experimental software sprints to integrate AI into data engineering workflows.",
                    "Mentored junior developers and helped them grow into diverse engineering responsibilities.",
                ],
            },
            {
                "title": "Senior Software Engineer",
                "company": "Nodes Inc",
                "start_date": "December 2023",
                "end_date": "September 2024",
                "location": "USA (Remote)",
                "highlights": [
                    "Fine-tuned open-source models and designed evaluation benchmarks for accuracy and regression.",
                    "Set up local inference infrastructure for self-hosted model deployment, reducing API costs and latency.",
                    "Built data pipelines for large-scale LLM training and batch inference jobs.",
                    "Led third-party integrations for data ingestion and improved input quality for downstream models.",
                    "Integrated LangChain with OpenAI via Azure and designed LLM chains, REST APIs, and data flows.",
                ],
            },
            {
                "title": "Special Project Manager (Engineering)",
                "company": "Just Appraised",
                "start_date": "January 2022",
                "end_date": "February 2022",
                "location": "United States (Remote)",
                "highlights": [
                    "Configured product behavior with JSON to match customer requirements.",
                    "Parsed APIs and moved data into warehouses.",
                    "Worked with backend engineers to build loaders for database ingestion.",
                    "Supported technical configuration and growth engineering efforts.",
                ],
            },
            {
                "title": "Software Engineer",
                "company": "Online Remote Recruiting",
                "start_date": "February 2018",
                "end_date": "December 2021",
                "location": "United States (Remote)",
                "highlights": [
                    "Served as a founding software engineer on the team.",
                    "Built backend and AI infrastructure using NestJS and LangChain.",
                    "Set up ETL pipelines to improve batch data extraction.",
                    "Mentored junior engineers on the team.",
                ],
            },
            {
                "title": "Summer Intern",
                "company": "Preacher9",
                "start_date": "April 2016",
                "end_date": "April 2017",
                "location": "Pakistan",
                "highlights": [
                    "Developed and executed multi-channel digital marketing strategies.",
                    "Analyzed campaign data, extracted insights, and optimized strategies for ROI.",
                    "Led cross-functional teams for digital marketing campaigns.",
                ],
            },
        ],
        "projects": [
            {
                "name": "Autonomous coding harness",
                "description": (
                    "Linear-driven autonomous coding harness with parallel worktrees, Guardian, "
                    "Prober, Reviewer agents, and stop-hook auto-merge gating."
                ),
                "skills": [
                    "agents",
                    "software automation",
                    "workflow orchestration",
                    "code review",
                ],
            }
        ],
        "startups_advised": ["Besnosy", "Carrus.io", "iCamp NYC"],
        "education": [
            {
                "degree": "Bachelors in IR",
                "institution": "Bahria University Islamabad",
            }
        ],
        "positioning": {
            "short": (
                "Senior backend/software engineer who can build reliable APIs, data systems, "
                "LLM-powered backend workflows, and pragmatic demos for early-stage teams."
            ),
            "best_fit_companies": [
                "Developer tools",
                "AI infrastructure",
                "Backend-heavy B2B SaaS",
                "Agent platforms",
                "Data-heavy B2B SaaS",
                "Workflow automation",
                "Recruiting and talent systems",
            ],
            "prototype_advantage": (
                "Can turn a startup's one-liner into a working backend, API, integration, "
                "data pipeline, or demo workflow with credible production instincts."
            ),
        },
        "outreach_proof_points": [
            "6+ years building backend systems and full-stack applications.",
            "Hands-on LLM work: fine-tuning, evaluation benchmarks, local inference, LangChain, OpenAI, Azure.",
            "Data engineering lead experience with high-volume ETL and ingestion pipelines.",
            "Founding-engineer style experience building backend and AI infrastructure from scratch.",
            "Recent autonomous coding-agent harness work with parallel worktrees and review gates.",
            "Remote-first experience with US companies.",
        ],
        "source": {
            "resume_path": str(resume_path),
            "pages": pages,
            "raw_text_path": "",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "notes": [
                "Generated from resume PDF plus the user's stated expertise in LLMs, backend systems, and data engineering.",
                "Keep this file local/private; it contains personal contact information.",
            ],
        },
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Convert a resume PDF into candidate_profile.json."
    )
    parser.add_argument("--resume", type=Path, default=settings.resume_path)
    parser.add_argument("--profile", type=Path, default=settings.candidate_profile_path)
    parser.add_argument("--text", type=Path, default=settings.resume_text_path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raw_text, pages = extract_pdf_text(args.resume)
    args.text.parent.mkdir(parents=True, exist_ok=True)
    args.text.write_text(raw_text, encoding="utf-8")

    profile = build_profile(args.resume, raw_text, pages)
    profile["source"]["raw_text_path"] = str(args.text)
    write_json(args.profile, profile)

    print(f"Extracted {pages} pages from {args.resume}")
    print(f"Wrote {args.text}")
    print(f"Wrote {args.profile}")


if __name__ == "__main__":
    main()
