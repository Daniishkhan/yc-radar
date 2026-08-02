from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ci_keeps_checks_and_gates_manual_production_deployment() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "push:" in workflow
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "pull_request_target:" not in workflow
    assert "uv sync --extra dev --locked" in workflow
    assert "uv run alembic upgrade head" in workflow
    assert "uv run alembic check" in workflow
    assert "uv run pytest" in workflow
    assert "uv run ruff check src tests scripts migrations" in workflow

    deploy = workflow.split("  deploy-production:", maxsplit=1)[1]
    assert "github.event_name == 'workflow_dispatch'" in deploy
    assert "github.ref == 'refs/heads/main'" in deploy
    assert "needs: test" in deploy
    assert "environment: production" in deploy
    assert "id-token: write" in deploy
    assert "cancel-in-progress: false" in deploy
    assert 'deploy "${GITHUB_SHA}"' in deploy
    assert "AWS_ACCESS_KEY_ID" not in deploy
    assert "ssh " not in deploy.lower()


def test_github_role_can_only_run_the_bounded_worker_document() -> None:
    stack = (REPO_ROOT / "infra/aws/worker-stack.yaml").read_text(encoding="utf-8")
    deploy_role = stack.split("  GitHubActionsDeployRole:", maxsplit=1)[1].split(
        "  WorkerDataVolume:", maxsplit=1
    )[0]

    assert "token.actions.githubusercontent.com:aud: sts.amazonaws.com" in deploy_role
    assert (
        "token.actions.githubusercontent.com:sub: "
        "!Sub repo:${GitHubRepository}:environment:production"
    ) in deploy_role
    assert "Action: cloudformation:DescribeStacks" in deploy_role
    assert "Action: ssm:SendCommand" in deploy_role
    assert "Action: ssm:GetCommandInvocation" in deploy_role
    assert "${DeploymentDocument}" in deploy_role
    assert "${WorkerInstance}" in deploy_role
    assert "AWS-RunShellScript" not in deploy_role
    assert "ssm:StartSession" not in deploy_role

    document = stack.split("  DeploymentDocument:", maxsplit=1)[1].split(
        "  GitHubActionsDeployRole:", maxsplit=1
    )[0]
    assert 'allowedPattern: "^[0-9a-f]{40}$"' in document
    assert 'radar-deploy --revision "{{ Revision }}"' in document


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
    assert "DeploymentDocumentName" in ssm_helper
    assert '"Parameters": {"Revision": [sys.argv[4]]}' in ssm_helper
