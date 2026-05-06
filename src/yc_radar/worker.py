from __future__ import annotations

from celery import Celery
from kombu import Queue

from yc_radar.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "yc_radar",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["yc_radar.tasks.page_classification"],
)

celery_app.conf.update(
    accept_content=["json"],
    enable_utc=True,
    result_expires=settings.celery_task_result_expires,
    result_serializer="json",
    timezone="UTC",
    task_acks_late=True,
    task_default_queue="default",
    task_queues=(
        Queue("default"),
        Queue("discovery"),
        Queue("fetch"),
        Queue("classification"),
        Queue("job_extraction"),
        Queue("llm_enrichment"),
        Queue("embeddings"),
    ),
    task_routes={
        "yc_radar.classify_discovered_url": {"queue": "classification"},
    },
    task_serializer="json",
    task_track_started=True,
    worker_prefetch_multiplier=1,
)
