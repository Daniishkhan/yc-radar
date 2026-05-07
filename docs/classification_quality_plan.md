# Classification Quality Plan

The first full run proved the discovery -> queued classification mechanics work, but it also exposed
the next quality problem: a small number of broad job marketplaces or location-heavy job pages can
dominate the URL inventory.

Current production snapshot from the first RDS run:

```text
companies: 5880
discovery_completed: 5880
discovered_urls: 43213
source_documents: 26311
page_classifications: 26310
external_jobs: 2931
classification_queue_remaining: 16895
```

Early page-kind mix:

```text
career_home: 11292
fetch_error: 8284
job_detail: 2931
irrelevant: 2606
job_listing: 964
ats_listing: 221
unknown: 18
```

The important warning sign:

```text
11 companies account for 40322 of 43213 discovered URLs.
```

Examples:

```text
vahan: 19090
tsenta: 4401
lokal: 3697
kalibrr: 3371
powerus: 3245
landed-2: 2780
standout: 2302
```

Some of those URLs are legitimate pages, but many are marketplace, location, expired, or third-party
job inventory pages. We should treat the classification output as raw evidence until it passes a
quality gate.

## Goal

Build a post-classification quality layer before candidate ranking.

The next pipeline shape should be:

```text
raw discovered URLs
  -> source documents
  -> page classifications
  -> quality flags and filtered views
  -> candidate-fit shortlist
```

Do not feed raw `external_job_postings` directly into final ranking yet.

## Step 1: Add Quality Queries

Create a script such as:

```text
scripts/report_classification_quality.py
```

It should print and optionally write CSVs under `data/local/runs/quality/`.

Minimum reports:

```text
company_url_counts.csv
company_classification_counts.csv
company_error_rates.csv
external_job_role_fit_counts.csv
suspicious_companies.csv
sample_job_details.csv
```

Useful metrics per company:

```text
discovered_url_count
source_document_count
page_classification_count
fetch_error_count
fetch_error_rate
job_detail_count
job_listing_count
career_home_count
irrelevant_count
external_job_count
strong_job_count
possible_job_count
```

## Step 2: Flag Suspicious URL Floods

Add deterministic suspicious-company flags, initially computed in the report script. Persisting them
to a table can come later if the report proves useful.

Initial flags:

```text
url_flood: discovered_url_count > 100
job_detail_flood: job_detail_count > 100
high_fetch_error_rate: page_classification_count >= 20 and fetch_error_rate >= 0.5
marketplace_like_paths: many URLs under /jobs/<location-or-company-like-slug>
third_party_job_inventory: job titles or URLs mention many non-YC employer names
low_signal_mass: many career_home pages under job-looking paths
```

The key idea is not to delete these rows. Keep raw evidence, but exclude suspicious slices from
ranking until inspected.

## Step 3: Build A Filtered Job View

Create either a SQL view or a script-generated CSV for ranking input.

Proposed view name:

```text
candidate_external_job_postings
```

Initial inclusion rules:

```text
role_fit in ('strong', 'possible')
page_kind = 'job_detail'
http_status = 200
company is not url_flood unless allowlisted
company is not third_party_job_inventory unless allowlisted
title is not sales, account executive, recruiter, internship, customer support, courier, driver
```

Initial allowlist candidates:

```text
retool
scale-ai
posthog
model-ml
taxgpt
```

The allowlist should be data-driven later, but a simple local config is fine for the first pass.

## Step 4: Inspect Representative Samples

Before trusting the filters, inspect samples from each bucket:

```text
20 strong jobs from non-suspicious companies
20 possible jobs from non-suspicious companies
20 strong jobs from suspicious companies
20 job_detail pages classified as weak/exclude
20 career_home pages under /jobs/ paths
20 fetch_error rows from top error companies
```

For each sample, decide:

```text
keep
exclude company
exclude URL pattern
classification rule bug
role-fit rule bug
needs LLM cleanup later
```

## Step 5: Tighten Discovery Rules

After the report identifies repeat offenders, update `scripts/discover_career_urls.py`.

Likely improvements:

```text
cap sitemap URL hits per company before inserting discovered_urls
skip location-heavy job URL patterns for known marketplaces
prefer listing/home pages over thousands of individual location pages
require stronger job-detail evidence for non-ATS /jobs/<slug> pages
record capped/skipped counts in discovery evidence
```

Do not hard-delete existing prod rows. Future runs can overwrite or supersede the inventory.

## Step 6: Tighten Classification Rules

Update `scripts/classify_discovered_urls.py` after sample inspection.

Likely improvements:

```text
classify location/courier pages as irrelevant or low-priority job_detail
detect third-party employer inventory from title/URL patterns
avoid promoting suspicious marketplace pages into external_job_postings
store quality flags in page_classifications.evidence
```

The first safe place to store these signals is `page_classifications.evidence`.

## Step 7: Produce The First Useful Shortlist

Once filtered views exist, generate a shortlist from filtered jobs only:

```text
strong backend/platform/infrastructure jobs
possible backend/SWE jobs
companies hiring but without clean job details
companies with promising career pages but no extracted job yet
```

Output:

```text
data/local/runs/<timestamp>/candidate_external_jobs.csv
data/local/runs/<timestamp>/company_quality_report.csv
data/local/runs/<timestamp>/shortlist.csv
```

## Acceptance Criteria

The quality gate is good enough when:

```text
top suspicious companies are visible immediately
raw and filtered counts are both reported
filtered strong/possible jobs can be inspected in TablePlus
the shortlist is not dominated by marketplaces or location pages
we can explain why each excluded company or URL pattern was excluded
```

This should happen before adding more sources, embeddings, or LLM enrichment.
