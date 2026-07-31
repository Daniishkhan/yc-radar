# Google Cloud Vertex AI federation

The AWS worker uses Google Cloud Workload Identity Federation (WIF) and its EC2 instance profile
to obtain short-lived Google credentials. It never creates or installs a Google service-account
key.

The live resources are intentionally narrow:

- Google Cloud project `ai-project-jul-19` (`64318475882`);
- dedicated service account
  `radar-domain-resolver@ai-project-jul-19.iam.gserviceaccount.com`;
- service-account project role `roles/aiplatform.user`;
- workload identity pool `radar-aws` and AWS provider `radar-worker`;
- AWS account `211236627350`;
- the exact EC2 role emitted by the `radar-worker` CloudFormation stack. On 2026-07-31 that role
  is `radar-worker-WorkerRole-NOqPlF4c7Z8Z`.

The provider condition admits only an STS assumed-role ARN beginning with the configured account
and exact role name. The service account separately grants `roles/iam.workloadIdentityUser` only
to the provider's normalized full `attribute.aws_role` ARN principal set. The provisioner derives
the current physical role from CloudFormation rather than freezing its random suffix in code.

## Reconcile the Google Cloud resources

The administrator running the provisioner needs permission to enable project services, manage
workload identity pools, create/manage the dedicated service account, and change project/service
account IAM. The AWS identity used for discovery needs read access to the worker stack and must be
in account `211236627350`.

Without `--apply`, the script is plan-only and makes no cloud calls:

```bash
./infra/gcp/provision-vertex-wif.sh \
  --project ai-project-jul-19 \
  --aws-profile radar-athena \
  --aws-region us-east-1
```

After reviewing both active CLI identities, reconcile the APIs, service account, least-privilege
roles, pool/provider restrictions, and ignored credential config:

```bash
./infra/gcp/provision-vertex-wif.sh \
  --project ai-project-jul-19 \
  --aws-profile radar-athena \
  --aws-region us-east-1 \
  --output data/local/gcp/gcp-wif.json \
  --apply
```

The output file is an external-account ADC configuration. It contains endpoint and resource
identifiers, not a private key, AWS credential, access token, or refresh token. Its default path
is under ignored `data/local/` so it cannot accidentally become part of the Docker build context
or a Git commit. Do not replace this flow with `gcloud iam service-accounts keys create`.

The script is convergent: existing resources are updated to the declared account, current stack
role, Google's normalized AWS-role mapping, and exact ARN condition. It removes only obsolete
`radar-aws` `attribute.aws_role` impersonation bindings from this dedicated service account; other
IAM bindings are preserved. A replacement CloudFormation role therefore requires a deliberate
rerun before the new worker can exchange credentials. Existing tokens remain bounded by their
short lifetime.

Google's reference flow and the checked-in command both use an AWS external-account config with
IMDSv2 and service-account impersonation. See [Google's AWS VM federation guide][wif-guide] and
[external-account credential format][aip-4117].

[wif-guide]: https://cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds
[aip-4117]: https://google.aip.dev/auth/4117

## Install the non-secret config on the worker

Deploy the revision containing the host installer, then send the generated config through SSM:

```bash
./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  deploy

./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  gcp-wif ai-project-jul-19 data/local/gcp/gcp-wif.json

./infra/aws/worker-ssm.sh \
  --profile radar-athena \
  --region us-east-1 \
  gcp-wif-status
```

The installer rejects configs with an unexpected pool, provider, service account, token endpoint,
credential source, or missing IMDSv2 endpoint. It atomically installs the validated file at
`/srv/radar/config/gcp/gcp-wif.json` with read-only-to-the-app permissions. Compose mounts that
directory read-only at `/etc/radar`; the app runs as UID 1000 and reads
`/etc/radar/gcp-wif.json`. The file is not copied into the image.

The retained runtime environment is reconciled to:

```dotenv
GOOGLE_CLOUD_PROJECT=ai-project-jul-19
GOOGLE_CLOUD_LOCATION=global
YC_RADAR_VERTEX_MODEL=gemini-3.5-flash-lite
GOOGLE_APPLICATION_CREDENTIALS=/etc/radar/gcp-wif.json
```

The AWS credential source remains the instance profile. The container can use IMDSv2 because the
EC2 metadata response hop limit is two; no AWS access keys are copied into the container or config.

## Billing guardrail

A Cloud Billing budget is recommended but is not part of this provisioner. Budget creation is a
billing-account or project-scoped administrative action, and the current Google user does not have
the necessary billing-account IAM. Ask a billing administrator to create an alert for project
`ai-project-jul-19` in the Cloud Billing console or grant the narrowly appropriate budget role.
`roles/billing.costsManager` on the billing account includes budget management, but also permits
viewing and exporting billing cost data; do not grant it to the runtime service account.

Budgets alert on delayed billing data and are not hard request caps. The 100-company calibration
in the worker runbook is the first operational cost boundary.

[Google's budget documentation](https://cloud.google.com/billing/docs/how-to/budgets) lists the
current project- and billing-account-level permissions.
