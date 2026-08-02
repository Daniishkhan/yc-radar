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
