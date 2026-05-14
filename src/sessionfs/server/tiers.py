"""Tier definitions and feature gating."""

from __future__ import annotations

from enum import Enum


class Tier(str, Enum):
    FREE = "free"
    STARTER = "starter"
    PRO = "pro"
    TEAM = "team"
    ENTERPRISE = "enterprise"


TIER_ORDER = {
    Tier.FREE: 0,
    Tier.STARTER: 1,
    Tier.PRO: 2,
    Tier.TEAM: 3,
    Tier.ENTERPRISE: 4,
}

TIER_FEATURES: dict[Tier, dict] = {
    Tier.FREE: {
        "features": set(),
        "storage_bytes": 0,
        "description": "Capture + resume + local search only",
    },
    Tier.STARTER: {
        "features": {
            "cloud_sync",
            "mcp_local",
            "dashboard",
            "bookmarks_cloud",
            "aliases_cloud",
            "judge_manual",
            "summary_deterministic",
        },
        "storage_bytes": 500 * 1024 * 1024,  # 500 MB
        "description": "Cloud sync + dashboard + manual judge",
    },
    Tier.PRO: {
        "features": {
            "cloud_sync",
            "autosync",
            "mcp_local",
            "mcp_remote",
            "dashboard",
            "bookmarks_cloud",
            "aliases_cloud",
            "judge_manual",
            "judge_auto",
            "auto_audit",
            "summary_deterministic",
            "summary_narrative",
            "handoff",
            "pr_comments",
            "mr_comments",
            "project_context",
            "dlp_secrets",
            "custom_base_url",
            "agent_personas",
        },
        "storage_bytes": 500 * 1024 * 1024,  # 500 MB
        "description": "Everything including DLP and auto-audit",
    },
    Tier.TEAM: {
        "features": {
            "cloud_sync",
            "autosync",
            "mcp_local",
            "mcp_remote",
            "dashboard",
            "bookmarks_cloud",
            "aliases_cloud",
            "judge_manual",
            "judge_auto",
            "auto_audit",
            "summary_deterministic",
            "summary_narrative",
            "handoff",
            "pr_comments",
            "mr_comments",
            "project_context",
            "dlp_secrets",
            "custom_base_url",
            "team_management",
            "shared_storage_pool",
            "org_settings",
            "agent_personas",
            "agent_tickets",
            "agent_runs",
        },
        "storage_bytes": 1024 * 1024 * 1024,  # 1 GB per user
        "description": "Pro + team management",
    },
    Tier.ENTERPRISE: {
        "features": {
            "cloud_sync",
            "autosync",
            "mcp_local",
            "mcp_remote",
            "dashboard",
            "bookmarks_cloud",
            "aliases_cloud",
            "judge_manual",
            "judge_auto",
            "auto_audit",
            "summary_deterministic",
            "summary_narrative",
            "handoff",
            "pr_comments",
            "mr_comments",
            "project_context",
            "dlp_secrets",
            "custom_base_url",
            "team_management",
            "shared_storage_pool",
            "org_settings",
            "agent_personas",
            "agent_tickets",
            "agent_runs",
            "dlp_hipaa",
            "security_dashboard",
            "policy_engine",
            "saml_sso",
            "compliance_exports",
            "audit_retention_6yr",
            "breach_notifications",
            "custom_patterns",
        },
        "storage_bytes": 0,  # Unlimited (self-hosted)
        "description": "Self-hosted + HIPAA + governance",
    },
}


def get_features_for_tier(tier: Tier | str) -> set[str]:
    """Get all features available for a tier."""
    if isinstance(tier, str):
        try:
            tier = Tier(tier)
        except ValueError:
            # Handle legacy 'admin' tier — treat as enterprise
            if tier == "admin":
                tier = Tier.ENTERPRISE
            else:
                return set()
    return set(TIER_FEATURES[tier]["features"])


def get_storage_limit(tier: Tier | str) -> int:
    """Get storage limit in bytes for a tier."""
    if isinstance(tier, str):
        try:
            tier = Tier(tier)
        except ValueError:
            if tier == "admin":
                return 0  # Unlimited
            return 0
    return TIER_FEATURES[tier]["storage_bytes"]


def get_minimum_tier_for_feature(feature: str) -> str:
    """Return the lowest tier that includes a given feature."""
    for tier in (Tier.FREE, Tier.STARTER, Tier.PRO, Tier.TEAM, Tier.ENTERPRISE):
        if feature in TIER_FEATURES[tier]["features"]:
            return tier.value
    return "enterprise"


def format_bytes(n: int) -> str:
    """Human-readable byte count."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n //= 1024
    return f"{n} PB"
