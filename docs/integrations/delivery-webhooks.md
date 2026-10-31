# GitHub / GitLab delivery webhooks

SessionFS ingests pull requests, merge requests, releases and deployments
through per-org, per-provider inbound webhooks. These feed the ticket ↔ PR
links, the releases/deployments ledger and the GitLab merge-readiness commit
status that make up the Review Loop's delivery story.

Unlike the Jira and Confluence integrations, delivery webhooks are **not
feature-gated** — every org gets them. They are managed from the CLI or the
API by an org owner or admin.

## What it does

When GitHub or GitLab sends a delivery event, SessionFS:

1. Maps the event's repository remote to a project that lives in the same
   org (a repo owned by a *different* org is ignored — never mapped
   cross-org).
2. Upserts the pull request / merge request and auto-links it to any ticket
   whose id (`tk_…`) or tracker key appears in the title, body or branch.
3. Records `release` events into the releases ledger and successful
   `deployment` / `deployment_status` events into the deployments ledger.
4. On a GitLab merge-request open/update, posts a **SessionFS review loop**
   commit status on the head commit (see below).

Every write is an idempotent upsert, so a re-delivery is a no-op at the data
layer even before the idempotency ledger short-circuits it.

## Setup

1. **Rotate the provider secret** (org admin):

   ```bash
   sfs org delivery-webhook rotate --provider github
   sfs org delivery-webhook rotate --provider gitlab
   ```

   The command prints the secret **once** plus the inbound URL. The secret is
   stored Fernet-encrypted at rest; only its last 4 characters are ever shown
   again. The same action is available through
   `POST /api/v1/orgs/{org_id}/delivery-webhooks/{provider}/rotate`.

2. **Register the webhook** with the returned URL and secret:

   - **GitHub** — org or repo webhook with the `Pull requests`, `Releases` and
     `Deployment statuses` events, content type `application/json`, and the
     secret as the webhook secret.
   - **GitLab** — project webhook with the `Merge request events`,
     `Release events` and `Deployment events` triggers, and the secret as the
     webhook's **Secret token**.

   The inbound URL is
   `https://<your-sessionfs-domain>/api/v1/webhooks/github/{org_id}` or
   `…/gitlab/{org_id}`. The dashboard shows the full URL for your org, and
   `sfs org delivery-webhook status --provider github|gitlab` prints it too.

## Inbound webhook

The endpoints live under `/api/v1` so the same URL works on the hosted
service and on a Helm install (whose dashboard proxy forwards `/api`, `/mcp`
and `/health`):

```
POST /api/v1/webhooks/github/{org_id}
POST /api/v1/webhooks/gitlab/{org_id}
```

Authentication fails closed:

- GitHub signs each delivery with `X-Hub-Signature-256: sha256=<hex>` (HMAC
  over the raw body); GitLab compares the `X-Gitlab-Token` header to the
  secret in constant time.
- An unknown org answers with the **same** `401` body as a bad signature —
  there is no existence oracle.
- A known org without a secret for that provider answers `503`.

The body is read as a stream and refused with `413` once it passes 1 MiB.
Deliveries are deduplicated on `X-GitHub-Delivery` / `X-Gitlab-Event-UUID`
(an idempotency ledger keyed per org/provider), so a provider retry is a
no-op. An event whose repository maps to no in-org project is ignored with
`200`.

## GitLab commit status

On a merge request open/update whose head commit is linked to at least one
ticket, SessionFS posts a **SessionFS review loop** status to the head commit
through the project owner's GitLab integration. The state is `success` only
when every linked ticket is merge-ready for that exact commit (a trusted
strict `VERIFIED-CLEAN` from an identity other than the implementer, no
harmful open findings); otherwise it is `pending`. The status name and
description carry no emails or dollar amounts.

The status push is best-effort: a missing integration or a failed API call
is logged and never fails the webhook. The GitLab client re-checks the
instance's SSRF guard before each request and never follows redirects.

## API reference

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/orgs/{org_id}/delivery-webhooks/{provider}/rotate` | Generate/rotate the provider secret (returned once; stored encrypted, only the last 4 characters shown again) |
| GET | `/api/v1/orgs/{org_id}/delivery-webhooks/{provider}/status` | Whether a secret exists, its last-4 hint, and the inbound URL |
| POST | `/api/v1/webhooks/github/{org_id}` | GitHub inbound webhook (HMAC-SHA256, idempotency-keyed) |
| POST | `/api/v1/webhooks/gitlab/{org_id}` | GitLab inbound webhook (token, idempotency-keyed) |

`provider` is `github` or `gitlab`.
