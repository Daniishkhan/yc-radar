import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "query_commoncrawl_greenhouse.py"
)
SPEC = importlib.util.spec_from_file_location("query_commoncrawl_greenhouse", SCRIPT_PATH)
assert SPEC and SPEC.loader
query_script = importlib.util.module_from_spec(SPEC)
sys.modules["query_commoncrawl_greenhouse"] = query_script
SPEC.loader.exec_module(query_script)


SCOPE = {
    "region": "us-east-1",
    "workgroup": "radar-commoncrawl",
    "database": "radar_commoncrawl",
    "crawl": "CC-MAIN-2026-30",
}


def succeeded(query_id: str) -> dict:
    return {
        "QueryExecution": {
            "QueryExecutionId": query_id,
            "Status": {"State": "SUCCEEDED"},
            "ResultConfiguration": {
                "OutputLocation": f"s3://results/{query_id}.csv"
            },
            "Statistics": {"DataScannedInBytes": 123},
        }
    }


def run_query(client, manifest: dict, manifest_path: Path):
    return query_script.run_query(
        "SELECT 1",
        stage="export_candidates",
        client=client,
        manifest=manifest,
        manifest_path=manifest_path,
        region="us-east-1",
        workgroup="radar-commoncrawl",
        database="radar_commoncrawl",
        poll_seconds=0,
        max_api_errors=1,
    )


def test_client_uses_instance_role_chain_when_profile_is_omitted(monkeypatch) -> None:
    session_options = []
    requested_clients = []

    class FakeSession:
        def client(self, service_name, **kwargs):
            requested_clients.append((service_name, kwargs))
            return service_name

    def fake_session(**kwargs):
        session_options.append(kwargs)
        return FakeSession()

    monkeypatch.setattr(query_script.boto3, "Session", fake_session)

    clients = query_script.make_aws_clients(profile=None, region="us-east-1")

    assert session_options == [{"region_name": "us-east-1"}]
    assert clients == ("athena", "s3")
    assert [name for name, _ in requested_clients] == ["athena", "s3"]


def test_restart_polls_recorded_query_id_without_resubmitting(tmp_path: Path) -> None:
    manifest_path = tmp_path / "candidates.csv.manifest.json"
    manifest = query_script.load_or_create_manifest(manifest_path, SCOPE)

    class InterruptedAthena:
        def __init__(self):
            self.start_calls = []

        def start_query_execution(self, **kwargs):
            self.start_calls.append(kwargs)
            return {"QueryExecutionId": "query-running"}

        def get_query_execution(self, **kwargs):
            assert kwargs == {"QueryExecutionId": "query-running"}
            raise ConnectionError("instance restarted")

    first_client = InterruptedAthena()
    with pytest.raises(RuntimeError, match="polling failed"):
        run_query(first_client, manifest, manifest_path)

    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    stage = on_disk["stages"]["export_candidates"]
    assert stage["query_execution_id"] == "query-running"
    assert len(first_client.start_calls[0]["ClientRequestToken"]) == 64

    class ResumedAthena:
        def start_query_execution(self, **kwargs):
            raise AssertionError(f"query was incorrectly resubmitted: {kwargs}")

        def get_query_execution(self, **kwargs):
            assert kwargs == {"QueryExecutionId": "query-running"}
            return succeeded("query-running")

    reloaded = query_script.load_or_create_manifest(manifest_path, SCOPE)
    result = run_query(ResumedAthena(), reloaded, manifest_path)

    assert result["QueryExecutionId"] == "query-running"
    assert reloaded["stages"]["export_candidates"]["state"] == "SUCCEEDED"


def test_start_retry_reuses_client_request_token_after_ambiguous_error(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "candidates.csv.manifest.json"
    manifest = query_script.load_or_create_manifest(manifest_path, SCOPE)

    class AmbiguousStartAthena:
        def __init__(self):
            self.tokens = []

        def start_query_execution(self, **kwargs):
            self.tokens.append(kwargs["ClientRequestToken"])
            if len(self.tokens) == 1:
                raise TimeoutError("response lost after acceptance")
            return {"QueryExecutionId": "same-query"}

        def get_query_execution(self, **kwargs):
            assert kwargs == {"QueryExecutionId": "same-query"}
            return succeeded("same-query")

    client = AmbiguousStartAthena()
    result = query_script.run_query(
        "SELECT 1",
        stage="export_candidates",
        client=client,
        manifest=manifest,
        manifest_path=manifest_path,
        region="us-east-1",
        workgroup="radar-commoncrawl",
        database="radar_commoncrawl",
        poll_seconds=0,
        max_api_errors=2,
    )

    assert result["QueryExecutionId"] == "same-query"
    assert len(client.tokens) == 2
    assert client.tokens[0] == client.tokens[1]


def test_retryable_athena_failure_gets_a_bounded_new_attempt(tmp_path: Path) -> None:
    manifest_path = tmp_path / "candidates.csv.manifest.json"
    manifest = query_script.load_or_create_manifest(manifest_path, SCOPE)

    class RetryableAthena:
        def __init__(self):
            self.query_ids = iter(["query-1", "query-2"])
            self.tokens = []

        def start_query_execution(self, **kwargs):
            self.tokens.append(kwargs["ClientRequestToken"])
            return {"QueryExecutionId": next(self.query_ids)}

        def get_query_execution(self, *, QueryExecutionId):
            if QueryExecutionId == "query-1":
                return {
                    "QueryExecution": {
                        "Status": {
                            "State": "FAILED",
                            "StateChangeReason": "transient capacity issue",
                            "AthenaError": {"Retryable": True},
                        }
                    }
                }
            return succeeded("query-2")

    client = RetryableAthena()
    result = run_query(client, manifest, manifest_path)

    assert result["QueryExecutionId"] == "query-2"
    assert len(client.tokens) == 2
    assert client.tokens[0] != client.tokens[1]
    assert len(manifest["stages"]["export_candidates"]["attempts"]) == 2


def test_s3_result_is_atomically_published_and_reused(tmp_path: Path) -> None:
    output = tmp_path / "candidates.csv"
    output.write_bytes(b"old-partial-data")
    manifest_path = tmp_path / "candidates.csv.manifest.json"
    manifest = query_script.load_or_create_manifest(manifest_path, SCOPE)

    class FakeS3:
        def __init__(self):
            self.calls = []

        def get_object(self, **kwargs):
            self.calls.append(kwargs)
            return {"Body": io.BytesIO(b"board_token\nacme\n")}

    client = FakeS3()
    query_script.download_result(
        client,
        output_location="s3://athena-results/path/query.csv",
        output=output,
        manifest=manifest,
        manifest_path=manifest_path,
    )
    query_script.download_result(
        client,
        output_location="s3://athena-results/path/query.csv",
        output=output,
        manifest=manifest,
        manifest_path=manifest_path,
    )

    assert output.read_bytes() == b"board_token\nacme\n"
    assert client.calls == [{"Bucket": "athena-results", "Key": "path/query.csv"}]
    assert manifest["download"]["state"] == "SUCCEEDED"
    assert manifest["download"]["bytes"] == len(output.read_bytes())


def test_manifest_fails_closed_when_query_scope_changes(tmp_path: Path) -> None:
    manifest_path = tmp_path / "candidates.csv.manifest.json"
    query_script.load_or_create_manifest(manifest_path, SCOPE)

    with pytest.raises(RuntimeError, match="does not match this query scope"):
        query_script.load_or_create_manifest(
            manifest_path,
            {**SCOPE, "crawl": "CC-MAIN-2026-26"},
        )
