"""Comprehensive tests for the SessionFS DLP scanner.

Covers PHI pattern detection (true positives + false negatives), secret pattern
regression, scan_dlp() category filtering / allowlist / custom patterns,
redact_text(), and validate_dlp_policy().
"""

from __future__ import annotations

import pytest

from sessionfs.security.secrets import (
    DLPFinding,
    PHI_PATTERNS,
    SECRET_PATTERNS,
    scan_dlp,
)
from sessionfs.server.dlp import (
    redact_text,
    validate_dlp_policy,
)


# =========================================================================
# Helper: scan only PHI or only secrets via the client-side scanner
# =========================================================================

def _phi_matches(text: str) -> list[DLPFinding]:
    return scan_dlp(text, categories=["phi"])


def _secret_matches(text: str) -> list[DLPFinding]:
    return scan_dlp(text, categories=["secrets"])


def _all_matches(text: str, **kwargs) -> list[DLPFinding]:
    return scan_dlp(text, categories=["secrets", "phi"], **kwargs)


# =========================================================================
# PHI Pattern Tests — True Positives
# =========================================================================


class TestPHITruePositives:
    """PHI patterns that MUST match."""

    def test_ssn(self):
        findings = _phi_matches("SSN: 123-45-6789")
        names = [f.pattern_name for f in findings]
        assert "ssn" in names

    def test_mrn_colon(self):
        findings = _phi_matches("MRN: 12345678")
        names = [f.pattern_name for f in findings]
        assert "mrn" in names

    def test_mrn_hash(self):
        findings = _phi_matches("mrn#99887766")
        names = [f.pattern_name for f in findings]
        assert "mrn" in names

    def test_dob_slash(self):
        findings = _phi_matches("DOB: 03/15/1985")
        names = [f.pattern_name for f in findings]
        assert "dob" in names

    def test_dob_long_form(self):
        findings = _phi_matches("date of birth: 12-25-1990")
        names = [f.pattern_name for f in findings]
        assert "dob" in names

    def test_npi(self):
        findings = _phi_matches("NPI: 1234567890")
        names = [f.pattern_name for f in findings]
        assert "npi_dea_license" in names

    def test_patient_name(self):
        findings = _phi_matches("patient: John Smith")
        names = [f.pattern_name for f in findings]
        assert "patient_name" in names

    def test_patient_phone(self):
        findings = _phi_matches("patient phone: (555) 123-4567")
        names = [f.pattern_name for f in findings]
        assert "patient_phone" in names

    def test_vin(self):
        findings = _phi_matches("VIN: 1HGBH41JXMN109186")
        names = [f.pattern_name for f in findings]
        assert "vin" in names

    def test_phi_url(self):
        findings = _phi_matches("https://ehr.hospital.com/patient/12345")
        names = [f.pattern_name for f in findings]
        assert "phi_url" in names


# =========================================================================
# PHI Pattern Tests — False Negatives (should NOT match)
# =========================================================================


class TestPHIFalseNegatives:
    """Inputs that should NOT be flagged as PHI."""

    def test_ssn_too_short(self):
        """SSN with only 3 digits in last group should not match."""
        findings = _phi_matches("ID: 123-45-678")
        ssn_findings = [f for f in findings if f.pattern_name == "ssn"]
        assert ssn_findings == []

    def test_date_without_dob_prefix(self):
        """A bare date without DOB/date-of-birth prefix should not match dob."""
        findings = _phi_matches("Meeting on 03/15/2024 at noon")
        dob_findings = [f for f in findings if f.pattern_name == "dob"]
        assert dob_findings == []

    def test_phone_without_medical_context(self):
        """Phone number without patient/emergency prefix should not match."""
        findings = _phi_matches("Call me at (555) 123-4567")
        phone_findings = [f for f in findings if f.pattern_name == "patient_phone"]
        assert phone_findings == []


# =========================================================================
# Secret Pattern Tests — Regression
# =========================================================================


class TestSecretPatterns:
    """Ensure existing secret patterns still detect known test vectors."""

    def test_aws_access_key(self):
        findings = _secret_matches("key = AKIAIOSFODNN7EXAMPLE rest")
        names = [f.pattern_name for f in findings]
        assert "aws_access_key_id" in names

    def test_openai_api_key(self):
        findings = _secret_matches("OPENAI_API_KEY=sk-abc123def456ghijklmnopqr")
        names = [f.pattern_name for f in findings]
        assert "openai_api_key" in names

    def test_database_url(self):
        findings = _secret_matches("DATABASE_URL=postgresql://user:pass@host/db")
        names = [f.pattern_name for f in findings]
        assert "database_url" in names

    def test_github_token(self):
        findings = _secret_matches("token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl")
        names = [f.pattern_name for f in findings]
        assert "github_token" in names

    def test_private_key(self):
        findings = _secret_matches("-----BEGIN RSA PRIVATE KEY-----")
        names = [f.pattern_name for f in findings]
        assert "private_key_pem" in names


# =========================================================================
# scan_dlp() — Category Filtering
# =========================================================================


class TestScanDLPCategories:
    """scan_dlp category parameter controls which pattern sets run."""

    MIXED_TEXT = "SSN: 123-45-6789\nkey=AKIAIOSFODNN7EXAMPLE end"

    def test_secrets_only_no_phi(self):
        findings = scan_dlp(self.MIXED_TEXT, categories=["secrets"])
        categories = {f.category for f in findings}
        assert "phi" not in categories
        assert "secret" in categories

    def test_phi_only_no_secrets(self):
        findings = scan_dlp(self.MIXED_TEXT, categories=["phi"])
        categories = {f.category for f in findings}
        assert "secret" not in categories
        assert "phi" in categories

    def test_both_categories(self):
        findings = scan_dlp(self.MIXED_TEXT, categories=["secrets", "phi"])
        categories = {f.category for f in findings}
        assert "secret" in categories
        assert "phi" in categories


# =========================================================================
# scan_dlp() — Allowlist
# =========================================================================


class TestScanDLPAllowlist:
    def test_builtin_allowlist_skips_our_keys(self):
        """Our own sk_sfs_ prefix keys should be allowlisted."""
        findings = scan_dlp("api_key=sk_sfs_testkey1234567890abcdef", categories=["secrets"])
        openai_findings = [f for f in findings if f.pattern_name == "openai_api_key"]
        assert openai_findings == []

    def test_extra_allowlist(self):
        """Caller-supplied allowlist entries should suppress findings."""
        text = "SSN: 123-45-6789"
        without = scan_dlp(text, categories=["phi"])
        assert any(f.pattern_name == "ssn" for f in without)

        with_allow = scan_dlp(text, categories=["phi"], allowlist=["123-45-6789"])
        ssn_findings = [f for f in with_allow if f.pattern_name == "ssn"]
        assert ssn_findings == []


# =========================================================================
# scan_dlp() — Custom Patterns
# =========================================================================


class TestScanDLPCustomPatterns:
    def test_custom_pattern_detection(self):
        custom = [
            {
                "name": "internal_id",
                "regex": r"INTID-\d{8}",
                "category": "pii",
                "severity": "high",
            }
        ]
        findings = scan_dlp("ref INTID-20240101 done", custom_patterns=custom)
        names = [f.pattern_name for f in findings]
        assert "internal_id" in names
        hit = [f for f in findings if f.pattern_name == "internal_id"][0]
        assert hit.category == "pii"
        assert hit.severity == "high"

    def test_custom_pattern_allowlisted(self):
        custom = [
            {
                "name": "internal_id",
                "regex": r"INTID-\d{8}",
                "category": "pii",
                "severity": "high",
            }
        ]
        findings = scan_dlp(
            "ref INTID-20240101 done",
            custom_patterns=custom,
            allowlist=["INTID-20240101"],
        )
        assert [f for f in findings if f.pattern_name == "internal_id"] == []


# =========================================================================
# scan_dlp() — Edge Cases
# =========================================================================


class TestScanDLPEdgeCases:
    def test_empty_text(self):
        assert scan_dlp("") == []

    def test_line_numbers_correct(self):
        text = "line one\nSSN: 123-45-6789\nline three"
        findings = scan_dlp(text, categories=["phi"])
        ssn = [f for f in findings if f.pattern_name == "ssn"]
        assert ssn
        assert ssn[0].line_number == 2

    def test_multiple_findings_same_line(self):
        text = "SSN: 123-45-6789 MRN: 12345678"
        findings = scan_dlp(text, categories=["phi"])
        names = {f.pattern_name for f in findings}
        assert "ssn" in names
        assert "mrn" in names
        # Both on line 1
        assert all(f.line_number == 1 for f in findings if f.pattern_name in ("ssn", "mrn"))

    def test_finding_has_correct_fields(self):
        findings = scan_dlp("SSN: 123-45-6789", categories=["phi"])
        ssn = [f for f in findings if f.pattern_name == "ssn"][0]
        assert ssn.category == "phi"
        assert ssn.severity == "critical"
        assert ssn.line_number == 1
        assert "123-45-6789" in ssn.match_text


# =========================================================================
# redact_text() Tests
# =========================================================================


class TestRedactText:
    def test_single_finding_redacted(self):
        text = "SSN: 123-45-6789"
        findings = [
            DLPFinding(
                pattern_name="ssn",
                category="phi",
                severity="critical",
                line_number=1,
                match_text="123-45-6789",
                context="SSN: 123-45-6789",
            )
        ]
        result = redact_text(text, findings)
        assert "[REDACTED:ssn]" in result
        assert "123-45-6789" not in result

    def test_multiple_findings_redacted(self):
        text = "SSN: 123-45-6789 and MRN: 12345678"
        findings = [
            DLPFinding(
                pattern_name="ssn",
                category="phi",
                severity="critical",
                line_number=1,
                match_text="123-45-6789",
                context="",
            ),
            DLPFinding(
                pattern_name="mrn",
                category="phi",
                severity="critical",
                line_number=1,
                match_text="MRN: 12345678",
                context="",
            ),
        ]
        result = redact_text(text, findings)
        assert "[REDACTED:ssn]" in result
        assert "[REDACTED:mrn]" in result
        assert "123-45-6789" not in result
        assert "MRN: 12345678" not in result

    def test_non_finding_text_preserved(self):
        text = "Hello SSN: 123-45-6789 world"
        findings = [
            DLPFinding(
                pattern_name="ssn",
                category="phi",
                severity="critical",
                line_number=1,
                match_text="123-45-6789",
                context="",
            ),
        ]
        result = redact_text(text, findings)
        assert result.startswith("Hello ")
        assert result.endswith(" world")

    def test_no_findings_returns_original(self):
        text = "Nothing sensitive here"
        assert redact_text(text, []) == text

    def test_multiline_redaction(self):
        text = "Line 1\nSSN: 123-45-6789\nLine 3"
        findings = [
            DLPFinding(
                pattern_name="ssn",
                category="phi",
                severity="critical",
                line_number=2,
                match_text="123-45-6789",
                context="",
            ),
        ]
        result = redact_text(text, findings)
        assert "Line 1" in result
        assert "Line 3" in result
        assert "123-45-6789" not in result


# =========================================================================
# validate_dlp_policy() Tests
# =========================================================================


class TestValidateDLPPolicy:
    def test_valid_policy_passes(self):
        policy = {"enabled": True, "mode": "redact", "categories": ["secrets", "phi"]}
        result = validate_dlp_policy(policy)
        assert result["enabled"] is True
        assert result["mode"] == "redact"
        assert result["categories"] == ["phi", "secrets"]  # sorted

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid mode"):
            validate_dlp_policy({"mode": "destroy", "categories": ["secrets"]})

    def test_invalid_category_raises(self):
        with pytest.raises(ValueError, match="Invalid category"):
            validate_dlp_policy({"mode": "warn", "categories": ["pii"]})

    def test_missing_enabled_gets_default(self):
        policy = {"mode": "warn", "categories": ["secrets"]}
        result = validate_dlp_policy(policy)
        assert result["enabled"] is True  # default when key is missing

    def test_empty_categories_raises(self):
        with pytest.raises(ValueError, match="non-empty list"):
            validate_dlp_policy({"mode": "warn", "categories": []})

    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="must be a dict"):
            validate_dlp_policy("not a dict")

    def test_non_bool_enabled_raises(self):
        with pytest.raises(ValueError, match="must be a boolean"):
            validate_dlp_policy({"enabled": "yes", "mode": "warn", "categories": ["secrets"]})

    def test_all_valid_modes_accepted(self):
        for mode in ("warn", "redact", "block"):
            result = validate_dlp_policy({"mode": mode, "categories": ["secrets"]})
            assert result["mode"] == mode

    def test_deduplicates_categories(self):
        result = validate_dlp_policy(
            {"mode": "warn", "categories": ["secrets", "secrets", "phi"]}
        )
        assert result["categories"] == ["phi", "secrets"]
