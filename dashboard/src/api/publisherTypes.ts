// Marketplace publishers, verification and trusted publishing (marketplace v2 §4).

/** Public publisher block attached to listings. Badges come only from live
 *  verification rows on the server, never from the display name. */
export interface PublisherBlock {
  id: string;
  namespace: string;
  display_name: string;
  owner_type: 'org' | 'user' | 'system';
  status: 'active' | 'suspended' | 'tombstoned';
  verified: boolean;
  badges: string[];
}

export interface PublisherVerification {
  id: string;
  method: 'dns_txt' | 'github_org' | 'github_user';
  subject: string;
  subject_display: string;
  status: 'pending' | 'verified' | 'lapsed' | 'revoked';
  status_reason: string | null;
  badge: string | null;
  verified_at: string | null;
  last_checked_at: string | null;
  last_success_at: string | null;
  consecutive_failures: number;
  github_installation_id: number | null;
  reused_org_domain_verification: boolean;
  txt_record?: string;
  txt_host?: string;
}

export interface TrustedPublisher {
  id: string;
  provider: 'github';
  repository: string;
  repository_id: number;
  repository_owner_id: number;
  workflow: string;
  reusable_workflow: string | null;
  allow_workflow_run: boolean;
  environment: string | null;
  ref_pattern: string | null;
  package_name: string | null;
  created_at: string | null;
  revoked_at: string | null;
}

export interface PublisherDetail extends PublisherBlock {
  org_id: string | null;
  user_id?: string | null;
  created_at: string | null;
  public_publishing?: boolean;
  suspended_reason?: string | null;
  review_flags?: { rule: string; conflict: string }[];
  verifications?: PublisherVerification[];
  trusted_publishers?: TrustedPublisher[];
}

export interface PublisherList {
  items: PublisherDetail[];
  count: number;
}

export interface ClaimPublisherInput {
  namespace: string;
  display_name?: string;
  org_id?: string;
}

export type StartVerificationInput =
  | { method: 'dns_txt'; domain: string }
  | { method: 'github_org' | 'github_user'; installation_id: number };

export interface AddTrustedPublisherInput {
  repository: string;
  workflow: string;
  environment?: string;
  ref_pattern?: string;
  package_name?: string;
  repository_id?: number;
  repository_owner_id?: number;
  reusable_workflow?: string;
  allow_workflow_run?: boolean;
}

/** signature_metadata of a version published through trusted publishing:
 *  PUBLICATION provenance (which workflow run published it), not build
 *  provenance. `build_provenance` stays null until a verified attestation
 *  binds the archive digest to a build. */
export interface GithubOidcProvenance {
  kind: 'github_oidc';
  claim: 'publication';
  verification: 'oidc_token';
  build_provenance: null;
  attestation: null;
  issuer: string;
  published_from_repo: string | null;
  published_from_repo_id: number | null;
  published_from_owner: string | null;
  published_from_owner_id: number | null;
  publishing_workflow: string;
  publishing_workflow_ref: string | null;
  publishing_job_workflow_ref: string | null;
  publishing_ref: string | null;
  publishing_run_sha: string | null;
  publishing_environment: string | null;
  publishing_event: string | null;
  publishing_run_id: string | null;
  publishing_run_attempt: string | null;
  runner_environment: string | null;
  publishing_run_url: string | null;
  verified_at: string;
}
