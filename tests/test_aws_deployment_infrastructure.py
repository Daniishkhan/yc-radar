from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ci_keeps_checks_and_deploys_main_over_tailscale_ssh() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" not in workflow
    assert "pull_request_target:" not in workflow
    assert "uv sync --extra dev --locked" in workflow
    assert "uv run alembic upgrade head" in workflow
    assert "uv run alembic check" in workflow
    assert "uv run pytest" in workflow
    assert "uv run ruff check src tests scripts migrations" in workflow

    deploy = workflow.split("  deploy-production:", maxsplit=1)[1]
    assert "github.event_name == 'push'" in deploy
    assert "github.ref == 'refs/heads/main'" in deploy
    assert "needs: test" in deploy
    assert "environment: production" in deploy
    assert "cancel-in-progress: false" in deploy
    assert "id-token: write" not in deploy
    assert "Check whether this push is still current" in deploy
    assert "Recheck main after joining the tailnet" in deploy
    assert "repos/${GITHUB_REPOSITORY}/git/ref/heads/main" in deploy
    assert "steps.revision.outputs.deploy == 'true'" in deploy
    assert "steps.confirmed-revision.outputs.deploy == 'true'" in deploy
    assert "tailscale/github-action@306e68a486fd2350f2bfc3b19fcd143891a4a2d8" in deploy
    assert "secrets.TS_OAUTH_CLIENT_ID" in deploy
    assert "secrets.TS_OAUTH_SECRET" in deploy
    assert "tags: tag:ci" in deploy
    assert "ping: radar-worker" in deploy
    assert "tailscale ssh ubuntu@radar-worker" in deploy
    assert "--revision '${GITHUB_SHA}'" in deploy
    assert "configure-aws-credentials" not in deploy
    assert "AWS_ACCESS_KEY_ID" not in deploy
    assert "AWS_DEPLOY_ROLE_ARN" not in deploy


def test_worker_retains_no_ingress_and_ssm_recovery() -> None:
    stack = (REPO_ROOT / "infra/aws/worker-stack.yaml").read_text(encoding="utf-8")

    assert "SecurityGroupIngress: []" in stack
    assert "AmazonSSMManagedInstanceCore" in stack
    assert "Google Cloud Workload Identity Federation" not in stack
    assert "WorkerRoleName:" not in stack
    assert "WorkerRoleArn:" not in stack


def test_deployment_refuses_active_jobs_and_untested_revisions() -> None:
    bootstrap = (REPO_ROOT / "deploy/aws/bootstrap-host.sh").read_text(encoding="utf-8")
    host_deploy = (REPO_ROOT / "deploy/aws/deploy-host.sh").read_text(encoding="utf-8")
    ssm_helper = (REPO_ROOT / "infra/aws/worker-ssm.sh").read_text(encoding="utf-8")

    assert "--untracked-files=all" in bootstrap
    assert "--untracked-files=no" not in bootstrap

    active_job_guard = host_deploy.index("active_jobs=$(systemctl list-units")
    fetch = host_deploy.index('git -C "${APP_DIR}" fetch')
    assert active_job_guard < fetch
    assert "A managed Radar job is active" in host_deploy
    assert host_deploy.count("--untracked-files=all") == 2
    assert "--untracked-files=no" not in host_deploy
    assert "remote_revision" in host_deploy
    assert "not tested revision" in host_deploy
    assert "refs/remotes/origin/${RADAR_REPO_BRANCH}^{commit}" in host_deploy

    final_cleanliness_guard = host_deploy.rindex("--untracked-files=all")
    compose_build = host_deploy.index('"${compose[@]}" build')
    assert final_cleanliness_guard < compose_build

    assert "deploy REVISION" in ssm_helper
    assert "DeploymentDocumentName" not in ssm_helper
    assert '"DocumentName": "AWS-RunShellScript"' in ssm_helper
    assert "radar-deploy --revision '${revision}'" in ssm_helper


def test_recurring_pipeline_and_freshness_timers_are_installed_safely() -> None:
    bootstrap = (REPO_ROOT / "deploy/aws/bootstrap-host.sh").read_text(encoding="utf-8")
    host_deploy = (REPO_ROOT / "deploy/aws/deploy-host.sh").read_text(encoding="utf-8")
    refresh = (REPO_ROOT / "deploy/aws/run-pipeline-refresh.sh").read_text(
        encoding="utf-8"
    )
    freshness = (REPO_ROOT / "deploy/aws/check-pipeline-freshness.sh").read_text(
        encoding="utf-8"
    )
    refresh_service = (
        REPO_ROOT / "deploy/systemd/radar-pipeline-refresh.service"
    ).read_text(encoding="utf-8")
    refresh_timer = (
        REPO_ROOT / "deploy/systemd/radar-pipeline-refresh.timer"
    ).read_text(encoding="utf-8")
    freshness_service = (
        REPO_ROOT / "deploy/systemd/radar-pipeline-freshness.service"
    ).read_text(encoding="utf-8")
    freshness_timer = (
        REPO_ROOT / "deploy/systemd/radar-pipeline-freshness.timer"
    ).read_text(encoding="utf-8")
    production_compose = (REPO_ROOT / "compose.prod.yml").read_text(encoding="utf-8")

    for install_script in (bootstrap, host_deploy):
        assert "run-pipeline-refresh.sh" in install_script
        assert "check-pipeline-freshness.sh" in install_script
        assert "radar-pipeline-refresh.service" in install_script
        assert "radar-pipeline-refresh.timer" in install_script
        assert "radar-pipeline-freshness.service" in install_script
        assert "radar-pipeline-freshness.timer" in install_script

    assert "'THEIRSTACK_API_KEY='" in bootstrap
    assert "THEIRSTACK_API_KEY: ${THEIRSTACK_API_KEY:-}" in production_compose
    assert "mem_limit: 4608m" in production_compose

    migrations = host_deploy.index('"${compose[@]}" run --rm app alembic upgrade head')
    timer_start = host_deploy.index("systemctl enable --now")
    assert migrations < timer_start
    assert "'radar-pipeline-refresh.service'" in host_deploy
    assert "'radar-pipeline-freshness.service'" in host_deploy

    assert "python scripts/sync_job_sources.py" in refresh
    assert "--delay-seconds 2" in refresh
    assert "python scripts/generate_job_opportunities.py" in refresh
    assert "--limit 200000" in refresh
    assert "--queue-limit 500" in refresh
    assert "python scripts/generate_weekly_targets.py" in refresh
    assert "--no-verify-hiring" in refresh
    assert "--no-llm" in refresh
    assert "application_queue.json" in refresh
    assert "verification_queue.json" in refresh
    assert "company_outreach_queue" not in refresh.split(
        "run_stage application-url-validation", maxsplit=1
    )[1].split("run_stage application-pool-metrics", maxsplit=1)[0]
    assert "python scripts/validate_application_urls.py" in refresh
    assert "python scripts/report_application_pool.py" in refresh
    assert "application_pool_metrics.json" in refresh
    assert "import_theirstack_jobs.py" not in refresh

    assert "python scripts/check_pipeline_freshness.py" in freshness
    assert "--max-age-hours 24" in freshness
    assert "pipeline_freshness.json" in freshness

    assert "ExecStart=/usr/local/sbin/radar-run-pipeline-refresh" in refresh_service
    assert "OnCalendar=*-*-* 02:30:00 UTC" in refresh_timer
    assert "Persistent=true" in refresh_timer
    assert "ExecStart=/usr/local/sbin/radar-check-pipeline-freshness" in freshness_service
    assert "OnCalendar=hourly" in freshness_timer
    assert "Persistent=true" in freshness_timer


def test_managed_workloads_share_a_nonblocking_host_lock() -> None:
    host_deploy = (REPO_ROOT / "deploy/aws/deploy-host.sh").read_text(
        encoding="utf-8"
    )
    run_job = (REPO_ROOT / "deploy/aws/run-job.sh").read_text(encoding="utf-8")
    refresh = (REPO_ROOT / "deploy/aws/run-pipeline-refresh.sh").read_text(
        encoding="utf-8"
    )
    freshness = (REPO_ROOT / "deploy/aws/check-pipeline-freshness.sh").read_text(
        encoding="utf-8"
    )

    for script in (host_deploy, run_job, refresh, freshness):
        assert "WORKLOAD_LOCK=/run/lock/radar-workload.lock" in script
        assert "flock -n" in script

    assert "radar-deploy.lock" in host_deploy
    assert 'radar-job-${name}.lock' in run_job
    assert "radar-pipeline-refresh.lock" in refresh
    assert "Another Radar workload is active; refusing" in host_deploy
    assert "Another Radar workload is active; refusing" in run_job
    assert "Another Radar workload is active; refusing" in refresh
    assert "Another Radar workload is active; skipping" in freshness
    assert "exit 0" in freshness


def test_recurring_pipeline_stops_after_queue_generation_failure() -> None:
    refresh = (REPO_ROOT / "deploy/aws/run-pipeline-refresh.sh").read_text(
        encoding="utf-8"
    )

    application_queue = refresh.index("if ! run_stage application-and-verification-queues")
    application_abort = refresh.index('exit "${exit_code}"', application_queue)
    outreach_queue = refresh.index("if ! run_stage company-outreach-queue")
    outreach_abort = refresh.index('exit "${exit_code}"', outreach_queue)
    validation = refresh.index("run_stage application-url-validation")
    metrics = refresh.index("run_stage application-pool-metrics")

    assert application_queue < application_abort < outreach_queue
    assert outreach_queue < outreach_abort < validation < metrics
