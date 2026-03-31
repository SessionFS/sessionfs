"""Unit tests for tier definitions and feature gating."""

from __future__ import annotations

import pytest

from sessionfs.server.tiers import (
    Tier,
    format_bytes,
    get_features_for_tier,
    get_minimum_tier_for_feature,
    get_storage_limit,
)


class TestTierFeatures:
    def test_free_has_no_features(self):
        assert get_features_for_tier(Tier.FREE) == set()

    def test_starter_has_cloud_sync(self):
        features = get_features_for_tier(Tier.STARTER)
        assert "cloud_sync" in features
        assert "dashboard" in features
        assert "judge_manual" in features

    def test_starter_lacks_pro_features(self):
        features = get_features_for_tier(Tier.STARTER)
        assert "autosync" not in features
        assert "handoff" not in features
        assert "auto_audit" not in features

    def test_pro_has_all_individual_features(self):
        features = get_features_for_tier(Tier.PRO)
        assert "autosync" in features
        assert "handoff" in features
        assert "pr_comments" in features
        assert "project_context" in features
        assert "custom_base_url" in features

    def test_team_has_team_management(self):
        features = get_features_for_tier(Tier.TEAM)
        assert "team_management" in features
        assert "shared_storage_pool" in features
        assert "org_settings" in features

    def test_enterprise_has_governance(self):
        features = get_features_for_tier(Tier.ENTERPRISE)
        assert "dlp_hipaa" in features
        assert "security_dashboard" in features
        assert "saml_sso" in features

    def test_string_tier_accepted(self):
        features = get_features_for_tier("pro")
        assert "handoff" in features

    def test_admin_maps_to_enterprise(self):
        features = get_features_for_tier("admin")
        assert "dlp_hipaa" in features

    def test_invalid_tier_returns_empty(self):
        assert get_features_for_tier("nonexistent") == set()


class TestStorageLimits:
    def test_free_has_zero_storage(self):
        assert get_storage_limit(Tier.FREE) == 0

    def test_starter_has_500mb(self):
        assert get_storage_limit(Tier.STARTER) == 500 * 1024 * 1024

    def test_pro_has_500mb(self):
        assert get_storage_limit(Tier.PRO) == 500 * 1024 * 1024

    def test_team_has_1gb(self):
        assert get_storage_limit(Tier.TEAM) == 1024 * 1024 * 1024

    def test_enterprise_unlimited(self):
        assert get_storage_limit(Tier.ENTERPRISE) == 0

    def test_admin_unlimited(self):
        assert get_storage_limit("admin") == 0


class TestMinimumTier:
    def test_cloud_sync_needs_starter(self):
        assert get_minimum_tier_for_feature("cloud_sync") == "starter"

    def test_handoff_needs_pro(self):
        assert get_minimum_tier_for_feature("handoff") == "pro"

    def test_team_management_needs_team(self):
        assert get_minimum_tier_for_feature("team_management") == "team"

    def test_saml_needs_enterprise(self):
        assert get_minimum_tier_for_feature("saml_sso") == "enterprise"

    def test_unknown_feature_returns_enterprise(self):
        assert get_minimum_tier_for_feature("nonexistent") == "enterprise"


class TestFormatBytes:
    def test_zero(self):
        assert format_bytes(0) == "0 B"

    def test_bytes(self):
        assert format_bytes(512) == "512 B"

    def test_megabytes(self):
        assert format_bytes(500 * 1024 * 1024) == "500.0 MB"

    def test_gigabytes(self):
        assert format_bytes(1024 * 1024 * 1024) == "1.0 GB"
