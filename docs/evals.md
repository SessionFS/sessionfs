# Evaluations and model/dataset lineage

Record-only evaluations (GitHub #121) and lineage (GitHub #122). SessionFS stores suite definitions, artifact pointers and the per-case results that graders post, computes each run's verdict and decides whether it is trusted. It never runs a grader, calls a model, holds a model key or fetches an artifact. User guide: https://sessionfs.dev/evals/.

Everything is project-scoped. Writes need the project's plan to include the `evaluations` feature (Team, Enterprise). Reads, suite deactivation and run void stay open after a downgrade; every artifact write (including deprecate, edge removal and hard delete) is plan-gated. Every miss (project, suite, run, artifact, edge) returns the same `404 Not found`, and visibility is checked before the plan gate.

## Roles and scopes

| Action | Who | Service key |
| --- | --- | --- |
| Read suites, cases, runs, results | project readers | `evals:read` |
| Create/edit suites, add/retire cases, disable a suite | project owner or org admin | refused (`403 service_key_not_allowed`) |
| Start a run, post results, finish | project editors | `evals:write` |
| Void a run | project owner or org admin | refused |
| Read artifacts, lineage, families, requirements, promotion check | project readers | `artifacts:read` (the family matrix also accepts `evals:read`) |
| Register, tag, link, unlink, deprecate | project editors | `artifacts:write` |
| Promote | project editors | refused |
| Set family requirements, hard delete | project owner or org admin | refused |

Service keys stay inside their org and project allowlist; a cross-org key gets the opaque 404.

## Suites and cases

| Route | Notes |
| --- | --- |
| `GET /api/v1/projects/{p}/eval-suites?cursor=&limit=` | Active suites by slug; `next_cursor` |
| `POST /api/v1/projects/{p}/eval-suites` | Body: the suite definition plus optional `cases` (≤ 500) for version 1 |
| `GET /api/v1/projects/{p}/eval-suites/{suite}` | `{suite}` = `evs_` version id, `evl_` lineage id (active version) or active slug; includes `versions` and `live_cases` |
| `PUT /api/v1/projects/{p}/eval-suites/{suite_id}` | New version of the definition; cases carry over; `slug` and `case_storage` are immutable |
| `DELETE /api/v1/projects/{p}/eval-suites/{suite_id}` | Deactivate (204) |
| `GET /api/v1/projects/{p}/eval-suites/{suite}/cases?cursor=&limit=` | Cases live in that version; `input`/`expected` only for inline storage |
| `POST /api/v1/projects/{p}/eval-suites/{suite}/cases` | `{cases: [...]}` ≤ 500; creates a new version |
| `POST /api/v1/projects/{p}/eval-suites/{suite}/cases/retire` | `{case_keys: [...]}`; creates a new version |
| `DELETE /api/v1/projects/{p}/eval-suites/{suite}/cases/{case_key}` | Retire one case; creates a new version |

Definition (validated by `sessionfs/eval_definition.py`, shared with the CLI; ≤ 32 KiB; unknown keys refused):

```json
{
  "slug": "clinical-notes",
  "name": "Clinical note summaries",
  "description": "",
  "grader": {"kind": "llm_judge", "persona": "clin-grader", "rubric": "", "script_ref": null, "model_hint": null},
  "acceptance": {
    "require_all_cases": true,
    "min_pass_rate": 0.95,
    "min_mean_score": null,
    "min_pass_rate_by_tag": {},
    "max_failures_by_tag": {"critical": 0},
    "no_regression_vs": "none",
    "regression_tags": ["critical"]
  },
  "case_storage": "inline",
  "case_bank_artifact_id": null
}
```

A case is live in version *v* when `added_in_version ≤ v` and `retired_in_version` is NULL or greater than *v*, so every version's case set stays reproducible. Case keys are unique per lineage and never reused. Limits: 200 active suites per project, 5,000 live cases per version, 500 per batch.

`content_sha256` is sha256 over the canonical JSON `{"expected": ..., "input": ...}` (sorted keys, compact separators, UTF-8), computed over the submitted text before any DLP redaction. In `reference` storage the case text is refused (`422 case_text_not_allowed`) and `content_sha256` is required.

Errors: `422 invalid_eval_suite`, `invalid_eval_case`, `duplicate_case_key`, `case_hash_mismatch`, `content_sha256_required`, `invalid_case_bank`, `slug_immutable`, `case_storage_immutable`; `409 suite_slug_taken`, `suite_limit`, `case_limit`, `case_exists`, `suite_superseded`, `suite_inactive`, `suite_stale`; `404 case_not_live`.

## Runs and results

| Route | Notes |
| --- | --- |
| `POST /api/v1/projects/{p}/eval-runs` | `{suite, artifact_id? \| subject_label? \| commit_sha?, ticket_id?, trigger_source: manual\|ci\|workflow\|promotion, trigger_ref?}`. Returns `run`, `suite`, `trust_preview`, the first 500 `cases`, `cases_next_cursor` |
| `GET /api/v1/projects/{p}/eval-runs?suite=&artifact_id=&state=&verdict=&cursor=&limit=` | Newest first |
| `GET /api/v1/projects/{p}/eval-runs/compare?a=&b=` | Same suite lineage required (`422 lineage_mismatch`) |
| `GET /api/v1/projects/{p}/eval-runs/{run}` | Lazily expires a run left running for 7 days |
| `GET /api/v1/projects/{p}/eval-runs/{run}/cases` | The pinned version's live cases |
| `GET /api/v1/projects/{p}/eval-runs/{run}/results?verdict=&tag=&cursor=` | By case key |
| `POST /api/v1/projects/{p}/eval-runs/{run}/results` | `{results: [...] ≤ 500, usage?}` |
| `POST /api/v1/projects/{p}/eval-runs/{run}/finish` | `{usage?}`; idempotent |
| `POST /api/v1/projects/{p}/eval-runs/{run}/void` | `{reason}` |

Start pins the active suite version, binds the calling credential (`grader_user_id`, plus `grader_service_key_id` for a service key) and creates an `agent_runs` row with `role = 'grader'` and `eval_run_id`. Results and finish need that exact credential (`403 grader_credential_mismatch`). Usage sent with `results` or `finish` is added to the grader run, so eval spend appears in the spend rollups.

Result row: `{case_key, verdict: pass|fail|error|skip, score?: 0..1, notes?: ≤ 4 KiB, output?: ≤ 16 KiB, output_sha256?, content_sha256?, grader_kind?}`. A replay of an identical body is counted as `replayed`; a different body for an already-graded case is `409 result_conflict` and nothing in the batch is written. In reference storage `content_sha256` is required and `output` is refused (send `output_sha256`).

Finish computes, under a row lock on the run, `summary` (`total_cases`, `graded`, `passed`, `failed`, `error`, `skip`, `missing`, `pass_rate` over graded cases, `mean_score`, `by_tag`, `regressions`, `baseline_run_id`, `reasons`) and `verdict`:

- `incomplete` when nothing is graded, or `require_all_cases` and some live case has no result;
- otherwise `failed` when any declared threshold misses or a regression is found, else `passed`. With no threshold declared, every graded case must pass.

`no_regression_vs: production` compares with the latest trusted passing run of the same lineage on the family's production version (no production version means nothing to compare). An explicit `evr_` baseline that is missing, voided or from another lineage fails the run.

**Trust.** `trusted = true` only when the bound credential is an active trusted reviewer for the pinned suite's `grader.persona` (project or org row; checked at start and finish), the grader's identities (a key counts as every user who minted or rotated it) do not intersect the producer's (artifact creator, the minters of the key that registered it, the credential of `produced_by_agent_run_id`, and a linked ticket's implementers), and a `human` grader used a user key. Anything unresolvable is untrusted, and so is a run whose subject names no producer at all (a commit-only or free-text subject without a ticket whose implementers resolve). Reasons: `grader_not_registered`, `grader_not_independent`, `producer_identity_unresolvable`, `producer_identity_unknown`, `grader_identity_unresolvable`, `human_grader_needs_user_key`, `suite_grader_unreadable`. An untrusted run never satisfies a gate or a baseline.

A finished run with a `ticket_id` writes one `ticket_events` row (`eval_run_finished` with verdict and trust); it never writes a verdict comment.

## Artifacts, lineage and promotion

| Route | Notes |
| --- | --- |
| `GET /api/v1/projects/{p}/artifacts?kind=&name=&stage=&tag=k[=v]&include_deprecated=&cursor=` | Newest first |
| `POST /api/v1/projects/{p}/artifacts` | `{kind, name, version, external_uri, digest?, schema_summary?, description?, tags?, produced_by_agent_run_id?}` |
| `GET /api/v1/projects/{p}/artifacts/{id}` | Adds `parents`, `children`, `latest_trusted_runs` |
| `PUT /api/v1/projects/{p}/artifacts/{id}/tags` | `{set: {k: v}, remove: [k]}`; ≤ 32 tags |
| `POST /api/v1/projects/{p}/artifacts/{id}/deprecate` | Kept, never promoted again |
| `DELETE /api/v1/projects/{p}/artifacts/{id}` | `409 artifact_in_use` while an edge, run or suite case bank references it |
| `GET /api/v1/projects/{p}/artifacts/{id}/lineage?direction=up\|down\|both&depth=1..25` | Bounded BFS, ≤ 500 nodes (`truncated`), with the latest trusted run per (suite lineage, node) |
| `GET /api/v1/projects/{p}/artifacts/{id}/promotion-check?to_stage=` | `{allowed, blockers}` |
| `POST /api/v1/projects/{p}/artifacts/{id}/promote` | `{to_stage}`; user keys only |
| `POST /api/v1/projects/{p}/artifact-edges` | `{parent_id, child_id, relation, context_ref?}` |
| `DELETE /api/v1/projects/{p}/artifact-edges/{edge}` | |
| `GET /api/v1/projects/{p}/artifact-families?kind=` | Version count, newest version, production version |
| `GET/PUT /api/v1/projects/{p}/artifact-families/{kind}/{name}/requirements` | `{suites: [slug \| evl_ \| evs_], require_tags: {k: v}}` |
| `GET /api/v1/projects/{p}/artifact-families/{kind}/{name}/evals` | Matrix: ≤ 50 versions × suite lineages, each cell the latest trusted run |

URI rules (`sessionfs/artifact_uri.py`, shared with the CLI): schemes `s3 gs mlflow models runs hf https vertex`, ≤ 2048 characters, no spaces or control characters, no userinfo, and no query key that carries a credential (`X-Amz-*`, `X-Goog-*`, `X-Ms-*`, `sig`, `signature`, `token`, `key`, `sv`, `se`, `sp`, anything containing `token`, `secret`, `password`, `signature`, `credential`, `apikey`) → `422 uri_has_credentials`; `422 invalid_uri_scheme`, `invalid_uri`. A URI the DLP policy would change is refused (`uri_has_sensitive_data`). `link_url` is set for `https` and `hf://` only.

Edges: both ends in the project, no self-edges (`422 self_edge`), no cycles (`422 lineage_cycle`, checked under the project scope lock; a lineage deeper than 25 levels or 500 nodes below the child is `422 lineage_too_deep`), ≤ 200 parents per child (`409 parent_limit`), duplicates `409 edge_exists`. ≤ 10,000 artifacts per project.

Promotion: allowed moves are candidate → staging, candidate/staging → production, any → archived, staging/archived → candidate. Staging and production need, for every required suite lineage of the family, a finished trusted `passed` run on this artifact id, and each required tag on every `trained_on` parent (a family with required tags and no `trained_on` parent is blocked). Misses are `409 promotion_blocked` with `blockers` (`suite_not_passed`, `parent_tag_missing`, `no_trained_on_parents`, `artifact_deprecated`, `invalid_transition`, `same_stage`). The stage change is a guarded `UPDATE ... WHERE stage = :from`; promoting to production archives the family's other production versions in the same transaction.

## DLP and retention

Before storage, under the strictest DLP union of the caller's orgs and the project org: suite name, description, rubric, script ref and model hint; case name, input, expected and rubric notes; result notes and output; artifact name, description, schema summary strings and tag values; edge context refs; run subject labels and trigger refs; void reasons. A scan failure is `422 dlp_unavailable`; a BLOCK policy is `422 dlp_blocked`.

`POST /api/v1/admin/sweeps/eval-outputs` (platform admin or a `platform_sweeps:run` key; opt-in Helm CronJob `api.evalOutputs`, daily) clears `output` and `notes` on results older than the org's `general.eval_output_retention_days` (1–3650, default 180; personal projects use the default), sets `output_cleared_at`, and keeps `verdict`, `score` and `output_sha256`. Keyset batches of 1,000.

## Schema (migrations 131–133)

- 131 `artifacts` (unique `(project_id, kind, name, version)`, kind/stage CHECKs), `artifact_edges` (unique `(parent_id, child_id, relation)`, no-self CHECK), `artifact_tags` (`(artifact_id, key)` PK).
- 132 `eval_suites` (one active row per lineage and one active slug per project, partial unique indexes), `eval_cases` (unique `(lineage_id, case_key)`), `artifact_family_requirements` (unique `(project_id, kind, name)`).
- 133 `eval_runs` (state/verdict/trigger CHECKs, partial gate index on finished runs), `eval_case_results` (unique `(run_id, case_id)`, score 0..1), `agent_runs.eval_run_id`, and `'grader'` in `ck_agent_run_role` (PostgreSQL only, where 106 created that CHECK).

Not in v1: approval-policy promotion, workflow `eval_pass` gates, org-scoped artifacts and cross-project edges, helper read tools and import adapters.
