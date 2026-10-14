"""Comprehensive tests for the SessionFS DLP scanner.

Covers PHI pattern detection (true positives + false negatives), secret pattern
regression, scan_dlp() category filtering / allowlist / custom patterns,
redact_text(), and validate_dlp_policy().
"""

from __future__ import annotations

import pytest

from sessionfs.security.secrets import (
    DLPFinding,
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


# =========================================================================
# redact_and_repack tier-aware member-size cap (v0.9.9.8 fix)
# =========================================================================


class TestRedactAndRepackMemberLimit:
    """Regression for the v0.9.9.7 release miss: redact_and_repack had a
    hardcoded 50 MB cap that silently nullified any
    SFS_MAX_SYNC_MEMBER_BYTES_PAID override above 50 MB for orgs with
    DLP=REDACT mode. The fix accepts a `member_limit_bytes` parameter
    and raises a typed DlpMemberTooLargeError so the route can return
    the same structured 413 envelope as _check_member_sizes.
    """

    @staticmethod
    def _build_archive(member_size: int, name: str = "messages.jsonl") -> bytes:
        """Construct a valid tar.gz with a single member of the given
        uncompressed size. The payload is highly compressible (single
        byte repeated) so the wire payload stays small.
        """
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            payload = b"x" * member_size
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        return buf.getvalue()

    def test_accepts_member_under_supplied_limit(self):
        """20 MB member with a 30 MB limit should redact + repack cleanly."""
        from sessionfs.server.dlp import redact_and_repack
        from sessionfs.security.secrets import DLPFinding

        archive = self._build_archive(member_size=20 * 1024 * 1024)
        # A finding is required so the function does work (it short-circuits
        # on empty findings and returns the input unchanged).
        # match_text must NOT appear in the payload — otherwise redact_text
        # grinds through millions of regex matches in O(payload_size).
        # The point of these tests is the member-size guard, not the
        # redaction work, so we use a sentinel that's absent from the
        # all-"x" payload.
        findings = [
            DLPFinding(
                pattern_name="dummy",
                match_text="not-in-payload-Z9Q",
                line_number=1,
                category="secrets",
                severity="low",
                context="",
            )
        ]
        result = redact_and_repack(
            archive,
            findings,
            member_limit_bytes=30 * 1024 * 1024,
        )
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_rejects_member_over_supplied_limit_with_typed_exception(self):
        """Pre-fix this raised generic ValueError. Post-fix it raises
        DlpMemberTooLargeError carrying member_name, member_size,
        limit_bytes — enough for the route to build the structured 413."""
        from sessionfs.server.dlp import (
            DlpMemberTooLargeError,
            redact_and_repack,
        )
        from sessionfs.security.secrets import DLPFinding

        archive = self._build_archive(member_size=11 * 1024 * 1024)
        # match_text must NOT appear in the payload — otherwise redact_text
        # grinds through millions of regex matches in O(payload_size).
        # The point of these tests is the member-size guard, not the
        # redaction work, so we use a sentinel that's absent from the
        # all-"x" payload.
        findings = [
            DLPFinding(
                pattern_name="dummy",
                match_text="not-in-payload-Z9Q",
                line_number=1,
                category="secrets",
                severity="low",
                context="",
            )
        ]
        with pytest.raises(DlpMemberTooLargeError) as exc_info:
            redact_and_repack(
                archive,
                findings,
                member_limit_bytes=10 * 1024 * 1024,
            )
        exc = exc_info.value
        assert exc.member_name == "messages.jsonl"
        assert exc.member_size == 11 * 1024 * 1024
        assert exc.limit_bytes == 10 * 1024 * 1024

    def test_accepts_60mb_member_when_limit_is_80mb(self):
        """The headline regression: pre-fix the hardcoded 50 MB rejected
        anything above 50 MB regardless of caller config. Post-fix, an
        80 MB limit (matching SFS_MAX_SYNC_MEMBER_BYTES_PAID=80MB)
        accepts a 60 MB member cleanly.
        """
        from sessionfs.server.dlp import redact_and_repack
        from sessionfs.security.secrets import DLPFinding

        archive = self._build_archive(member_size=60 * 1024 * 1024)
        # match_text must NOT appear in the payload — otherwise redact_text
        # grinds through millions of regex matches in O(payload_size).
        # The point of these tests is the member-size guard, not the
        # redaction work, so we use a sentinel that's absent from the
        # all-"x" payload.
        findings = [
            DLPFinding(
                pattern_name="dummy",
                match_text="not-in-payload-Z9Q",
                line_number=1,
                category="secrets",
                severity="low",
                context="",
            )
        ]
        result = redact_and_repack(
            archive,
            findings,
            member_limit_bytes=80 * 1024 * 1024,
        )
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_default_limit_is_conservative_when_caller_skips_param(self):
        """Backward compat: callers that don't pass member_limit_bytes
        get the conservative 10 MB default (matches free-tier cap).
        Production sync_push always passes the tier-resolved limit; the
        default only fires for unauthenticated / non-tier callers.
        """
        from sessionfs.server.dlp import (
            DEFAULT_DLP_MEMBER_LIMIT_BYTES,
            DlpMemberTooLargeError,
            redact_and_repack,
        )
        from sessionfs.security.secrets import DLPFinding

        assert DEFAULT_DLP_MEMBER_LIMIT_BYTES == 10 * 1024 * 1024

        # 11 MB member with NO limit specified → rejected via default.
        archive = self._build_archive(member_size=11 * 1024 * 1024)
        # match_text must NOT appear in the payload — otherwise redact_text
        # grinds through millions of regex matches in O(payload_size).
        # The point of these tests is the member-size guard, not the
        # redaction work, so we use a sentinel that's absent from the
        # all-"x" payload.
        findings = [
            DLPFinding(
                pattern_name="dummy",
                match_text="not-in-payload-Z9Q",
                line_number=1,
                category="secrets",
                severity="low",
                context="",
            )
        ]
        with pytest.raises(DlpMemberTooLargeError) as exc_info:
            redact_and_repack(archive, findings)
        assert exc_info.value.limit_bytes == DEFAULT_DLP_MEMBER_LIMIT_BYTES

    def test_rejects_member_that_grows_over_limit_after_redaction(self):
        """A member that starts under the cap can still exceed it after
        replacement expansion. The post-redaction size must be enforced,
        not just the original tar header size.
        """
        from sessionfs.server.dlp import (
            DlpMemberTooLargeError,
            redact_and_repack,
        )
        from sessionfs.security.secrets import DLPFinding

        limit = 1024 * 1024  # 1 MB
        # Starts well below the limit, but every "x" expands to the
        # 16-byte "[REDACTED:dummy]" marker, pushing the repacked member
        # above 1 MB.
        archive = self._build_archive(member_size=70 * 1024)
        findings = [
            DLPFinding(
                pattern_name="dummy",
                match_text="x",
                line_number=1,
                category="secrets",
                severity="low",
                context="",
            )
        ]

        with pytest.raises(DlpMemberTooLargeError) as exc_info:
            redact_and_repack(
                archive,
                findings,
                member_limit_bytes=limit,
            )
        exc = exc_info.value
        assert exc.member_name == "messages.jsonl"
        assert exc.member_size > limit
        assert exc.limit_bytes == limit


# =========================================================================
# redact_and_repack value-level redaction (GH #65)
# =========================================================================


class TestValueLevelRedaction:
    """Regression for the #65 bug: raw-text redaction over serialized JSON
    left unbalanced quote-escape sequences (a `database_url` match that
    swallowed the backslash escaping a closing `"`), corrupting
    messages.jsonl. The fix redacts at the decoded-VALUE level and
    re-serializes, so JSON stays valid; unchanged lines stay byte-identical;
    and lines that fail to parse as JSON fall back to raw-text redaction.
    """

    @staticmethod
    def _archive_with_messages(lines: list[str]) -> bytes:
        import io
        import tarfile

        payload = ("\n".join(lines) + "\n").encode("utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="messages.jsonl")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        return buf.getvalue()

    @staticmethod
    def _read_messages(archive: bytes) -> bytes:
        import io
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name == "messages.jsonl":
                    f = tar.extractfile(member)
                    assert f is not None
                    return f.read()
        raise AssertionError("messages.jsonl not present in repacked archive")

    def test_database_url_escaped_quotes_stays_valid_json(self):
        """A Terraform-style `database_url = "postgres://..."` inside a JSON
        string value (quotes JSON-escaped) must redact WITHOUT leaving the
        line unparseable — the pre-fix failure mode."""
        import json

        from sessionfs.server.dlp import redact_and_repack

        msg = {
            "msg_id": "m1",
            "role": "assistant",
            "content": [{
                "type": "text",
                "text": 'database_url = "postgresql://user:pass@db.example.com:5432/app"',
            }],
        }
        line = json.dumps(msg)
        # Sanity: the inner quotes really are JSON-escaped in the serialized line.
        assert '\\"' in line
        archive = self._archive_with_messages([line])

        findings = scan_dlp(line, categories=["secrets"])
        assert any(f.pattern_name == "database_url" for f in findings)

        repacked = redact_and_repack(archive, findings)
        repacked_lines = self._read_messages(repacked).decode("utf-8").splitlines()

        for ln in repacked_lines:
            json.loads(ln)  # must NOT raise — every line remains valid JSON
        joined = "\n".join(repacked_lines)
        assert "postgresql://user:pass@db.example.com:5432/app" not in joined
        assert "[REDACTED:database_url]" in joined

    def test_generic_password_assignment_escaped_quotes(self):
        import json

        from sessionfs.server.dlp import redact_and_repack

        msg = {
            "msg_id": "m2",
            "role": "assistant",
            "content": [{
                "type": "text",
                "text": 'password = "supersecretvalue123"',
            }],
        }
        line = json.dumps(msg)
        # Sanity: the inner quotes really are JSON-escaped, so the raw-text
        # scan (whose `['\"]` anchors miss `\"`) cannot see this assignment.
        assert '\\"' in line
        archive = self._archive_with_messages([line])

        findings = scan_dlp(line, categories=["secrets"])
        assert findings == []

        # The value-level re-scan catches it on the DECODED string. Pass
        # categories explicitly so the re-scan runs even with no raw findings.
        repacked = redact_and_repack(archive, findings, categories=["secrets"])
        repacked_lines = self._read_messages(repacked).decode("utf-8").splitlines()
        for ln in repacked_lines:
            json.loads(ln)
        joined = "\n".join(repacked_lines)
        assert "supersecretvalue123" not in joined
        assert "[REDACTED:generic_password_assignment]" in joined

    def test_raw_findings_are_still_redacted_when_rescan_misses_them(self):
        """Value-level redaction must be at least as strict as the old raw pass.

        A raw-scan finding the decoded re-scan cannot reproduce (here a custom
        pattern the re-scan is not configured with, whose match text in the
        serialized JSON carries an escaped quote) must still be redacted in
        its decoded spelling.
        """
        import json

        from sessionfs.server.dlp import DLPFinding, redact_and_repack

        line = json.dumps({"msg_id": "m9", "content": 'token is ab"cd-SECRETPART-99 ok'})
        archive = self._archive_with_messages([line])
        raw_match = 'ab\\"cd-SECRETPART-99'
        assert raw_match in line  # how it appears in the serialized line
        findings = [
            DLPFinding(
                pattern_name="custom_token",
                category="secret",
                severity="high",
                line_number=1,
                match_text=raw_match,
                context="",
            )
        ]
        repacked = redact_and_repack(archive, findings, categories=["secrets"])
        out = self._read_messages(repacked).decode("utf-8").splitlines()
        decoded = json.loads(out[0])["content"]
        assert "SECRETPART" not in decoded
        assert decoded == "token is [REDACTED:custom_token] ok"

    def test_secret_in_a_json_key_is_redacted(self):
        import json

        from sessionfs.server.dlp import redact_and_repack

        key = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
        line = json.dumps({"msg_id": "k1", "input": {key: "x", "other": "y"}})
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, [], categories=["secrets"])
        out = self._read_messages(repacked).decode("utf-8")
        assert key not in out
        record = json.loads(out.splitlines()[0])
        assert record["input"]["other"] == "y"
        assert "[REDACTED:openai_api_key]" in record["input"]

    def test_two_keys_redacting_to_the_same_marker_are_both_kept(self):
        import json

        from sessionfs.server.dlp import redact_and_repack

        k1 = "sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaa1111"
        k2 = "sk-proj-bbbbbbbbbbbbbbbbbbbbbbbbbbbb2222"
        line = json.dumps({"msg_id": "k2", "input": {k1: "one", k2: "two"}})
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, [], categories=["secrets"])
        record = json.loads(self._read_messages(repacked).decode("utf-8").splitlines()[0])
        assert sorted(record["input"].values()) == ["one", "two"]
        assert k1 not in json.dumps(record) and k2 not in json.dumps(record)

    def test_secret_held_as_a_number_is_redacted(self):
        import json

        from sessionfs.server.dlp import redact_and_repack

        line = json.dumps({"msg_id": "n1", "input": {"account": 987654321012}})
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(
            archive,
            [],
            categories=["secrets"],
            custom_patterns=[
                {"name": "acct", "regex": r"\b\d{12}\b", "category": "secret", "severity": "high"}
            ],
        )
        record = json.loads(self._read_messages(repacked).decode("utf-8").splitlines()[0])
        assert record["input"]["account"] == "[REDACTED:acct]"

    def test_decoded_text_exposes_quoted_secrets_the_raw_scan_misses(self):
        import json

        from sessionfs.security.secrets import scan_dlp
        from sessionfs.server.dlp import decoded_json_text

        line = json.dumps({"content": 'password = "supersecretvalue123"'})
        assert scan_dlp(line, categories=["secrets"]) == []
        found = scan_dlp(decoded_json_text(line), categories=["secrets"])
        assert {f.pattern_name for f in found} == {"generic_password_assignment"}

    def test_secret_held_as_a_json_field_pair_is_found_and_redacted(self):
        """{"password": "…"} is a key and a value in JSON, so neither the
        serialized line nor the bare value matches assignment patterns."""
        import json

        from sessionfs.security.secrets import scan_dlp
        from sessionfs.server.dlp import decoded_json_text, redact_and_repack

        line = json.dumps({"msg_id": "p1", "input": {"password": "supersecretvalue123", "user": "ok"}})
        assert scan_dlp(line, categories=["secrets"]) == []
        assert scan_dlp(decoded_json_text(line), categories=["secrets"])

        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, [], categories=["secrets"])
        record = json.loads(self._read_messages(repacked).decode("utf-8").splitlines()[0])
        assert record["input"]["password"] == "[REDACTED:generic_password_assignment]"
        assert record["input"]["user"] == "ok"

    def test_redacted_key_never_overwrites_an_existing_marker_key(self):
        import json

        from sessionfs.server.dlp import redact_and_repack

        secret_key = "sk-proj-cccccccccccccccccccccccccccc3333"
        line = json.dumps({
            "msg_id": "c1",
            "input": {secret_key: "one", "[REDACTED:openai_api_key]": "two"},
        })
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, [], categories=["secrets"])
        record = json.loads(self._read_messages(repacked).decode("utf-8").splitlines()[0])
        assert sorted(record["input"].values()) == ["one", "two"]

    def test_too_deeply_nested_json_is_uninspectable_not_clean(self):
        import pytest

        from sessionfs.server.dlp import (
            DlpUninspectableError,
            decoded_json_text,
            redact_and_repack,
        )

        deep = "[" * 5000 + "]" * 5000
        with pytest.raises(DlpUninspectableError):
            decoded_json_text(deep)
        moderately_deep = "[" * 300 + '"x"' + "]" * 300
        with pytest.raises(DlpUninspectableError):
            decoded_json_text(moderately_deep)
        archive = self._archive_with_messages([moderately_deep])
        with pytest.raises(ValueError):
            redact_and_repack(archive, [], categories=["secrets"])

    def test_field_pair_survives_quotes_backslashes_and_line_breaks(self):
        """Characters inside the value must not end the synthetic quoted value
        early and hide the rest of the secret from assignment patterns."""
        import json

        from sessionfs.security.secrets import scan_dlp
        from sessionfs.server.dlp import decoded_json_text, redact_and_repack

        for value in (
            'abc"abcdefghijk',
            "abc\\abcdefghijk",
            "abc\nabcdefghijk",
            "abc\u2028abcdefghijk",
            "abc\x1cabcdefghijk",
            "abc\x1dabcdefghijk",
            "abc\x1eabcdefghijk",
        ):
            line = json.dumps({"msg_id": "q", "input": {"password": value}})
            found = scan_dlp(decoded_json_text(line), categories=["secrets"])
            assert found, repr(value)
            archive = self._archive_with_messages([line])
            repacked = redact_and_repack(archive, [], categories=["secrets"])
            record = json.loads(self._read_messages(repacked).decode("utf-8").splitlines()[0])
            assert record["input"]["password"] == "[REDACTED:generic_password_assignment]", repr(value)

    def test_repeated_json_keys_are_uninspectable(self):
        import pytest

        from sessionfs.server.dlp import DlpUninspectableError, decoded_json_text, redact_and_repack

        line = '{"input": {"password": "abcdefghijk", "password": "ok"}}'
        with pytest.raises(DlpUninspectableError):
            decoded_json_text(line)
        archive = self._archive_with_messages([line])
        with pytest.raises(ValueError):
            redact_and_repack(archive, [], categories=["secrets"])

    def test_backslash_inside_a_quoted_password_is_still_detected(self):
        """The password pattern must not stop at a backslash: decoded text such
        as password = "abcd\\efghijkl" was caught before value-level redaction
        and must still be."""
        import json

        from sessionfs.security.secrets import scan_dlp
        from sessionfs.server.dlp import decoded_json_text, redact_and_repack

        text = 'password = "abcd\\efghijkl"'
        assert scan_dlp(text, categories=["secrets"])
        line = json.dumps({"msg_id": "b1", "content": text})
        assert scan_dlp(decoded_json_text(line), categories=["secrets"])
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, [], categories=["secrets"])
        out = self._read_messages(repacked).decode("utf-8")
        assert "efghijkl" not in out
        json.loads(out.splitlines()[0])

    def test_custom_pattern_written_against_serialized_json_is_removed(self):
        """A custom pattern can match JSON structure (field name + value). The
        value-level pass can't see it, so the residual sweep must remove the
        values it covers instead of reporting it redacted and storing it."""
        import json
        import re as _re

        from sessionfs.security.secrets import scan_dlp
        from sessionfs.server.dlp import redact_and_repack

        policy = [{
            "name": "employee_code",
            "regex": r'"employee_code"\s*:\s*"[^"]{8,}"',
            "category": "secret",
            "severity": "high",
        }]
        line = json.dumps({"msg_id": "e1", "input": {"employee_code": "EMP-77421-X", "team": "ops"}})
        archive = self._archive_with_messages([line])
        findings = scan_dlp(line, categories=["secrets"], custom_patterns=policy)
        assert findings
        repacked = redact_and_repack(archive, findings, categories=["secrets"], custom_patterns=policy)
        out = self._read_messages(repacked).decode("utf-8")
        assert "EMP-77421-X" not in out
        record = json.loads(out.splitlines()[0])
        assert record["input"]["team"] == "ops"
        assert not _re.search(policy[0]["regex"], out)

    def test_malformed_line_secret_with_unicode_escaped_quotes_is_removed(self):
        import json

        from sessionfs.security.secrets import scan_dlp
        from sessionfs.server.dlp import decoded_json_text, redact_and_repack

        line = '{"msg_id": "u1", "content": "password = \\u0022linesecretvalue99\\u0022",}'
        findings = scan_dlp(line + "\n" + decoded_json_text(line), categories=["secrets"])
        assert findings
        archive = self._archive_with_messages([json.dumps({"msg_id": "ok"}), line])
        repacked = redact_and_repack(archive, findings, categories=["secrets"])
        out = self._read_messages(repacked).decode("utf-8")
        assert "linesecretvalue99" not in out
        assert out.splitlines()[0] == json.dumps({"msg_id": "ok"})

    def test_reserialization_cannot_hide_a_structural_match(self):
        """Redacting one value re-serializes the line (whitespace changes), so
        a structural pattern must be re-checked against the NEW text."""
        import json
        import re as _re

        from sessionfs.security.secrets import scan_dlp
        from sessionfs.server.dlp import redact_and_repack

        policy = [
            {"name": "leak", "regex": r"LEAKONE123", "category": "secret", "severity": "high"},
            {
                "name": "employee_code",
                "regex": r'"employee_code"\s*:\s*"[^"]{8,}"',
                "category": "secret",
                "severity": "high",
            },
        ]
        line = json.dumps({"other": "LEAKONE123", "employee_code": "EMP-77421-X"})
        findings = scan_dlp(line, categories=["secrets"], custom_patterns=policy)
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, findings, categories=["secrets"], custom_patterns=policy)
        out = self._read_messages(repacked).decode("utf-8")
        assert "LEAKONE123" not in out and "EMP-77421-X" not in out
        assert not _re.search(policy[1]["regex"], out)
        json.loads(out.splitlines()[0])

    def test_malformed_line_keeps_context_for_later_patterns(self):
        """A first-pass match (here a custom rule on the word 'password') must
        not erase the context another pattern needs to find the secret."""
        from sessionfs.security.secrets import scan_dlp
        from sessionfs.server.dlp import decoded_json_text, redact_and_repack

        policy = [{"name": "pw_word", "regex": r"password", "category": "secret", "severity": "low"}]
        line = '{"content":"password = \\u0022supersecretvalue99\\u0022",}'
        findings = scan_dlp(
            line + "\n" + decoded_json_text(line), categories=["secrets"], custom_patterns=policy
        )
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, findings, categories=["secrets"], custom_patterns=policy)
        out = self._read_messages(repacked).decode("utf-8")
        assert "supersecretvalue99" not in out

    def test_existing_redaction_markers_are_not_reprocessed(self):
        """Re-pushing an already-redacted session leaves it byte-identical."""
        import json

        from sessionfs.server.dlp import redact_and_repack

        line = json.dumps({"msg_id": "r1", "content": 'password = "[REDACTED:generic_password_assignment]"'})
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, [], categories=["secrets"])
        assert self._read_messages(repacked).decode("utf-8").splitlines()[0] == line

    def test_overlapping_matches_redact_their_whole_union(self):
        """A short custom match inside a longer password assignment must not
        win and leave the rest of the secret in place."""
        import json

        from sessionfs.server.dlp import redact_and_repack

        policy = [{"name": "prefix", "regex": r"Xpassword", "category": "secret", "severity": "low"}]
        line = json.dumps({"msg_id": "o1", "content": 'Xpassword = "supersecretvalue123"'})
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, [], categories=["secrets"], custom_patterns=policy)
        out = self._read_messages(repacked).decode("utf-8")
        assert "supersecretvalue123" not in out
        json.loads(out.splitlines()[0])

    def test_span_serializer_matches_json_dumps_exactly(self):
        import json

        from sessionfs.server.dlp import _serialize, _serialize_with_spans

        samples = [
            {},
            [],
            {"a": 1, "b": [1, 2.5, None, True, "x\"y\n\u2028é"], "c": {}, "d": [], "e": {"f": [{}]}},
            [{"k": "v"}, [], [[]], "s", 0, -1.5e10, False],
            "just a string",
            42,
        ]
        for obj in samples:
            for indent in (None, 2):
                text, spans = _serialize_with_spans(obj, indent)
                assert text == _serialize(obj, indent)
                if indent is None:
                    assert text == json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                else:
                    assert text == json.dumps(obj, ensure_ascii=False, indent=2)
                for start, end, _path in spans:
                    json.loads(text[start:end])  # every span is one whole token

    def test_residual_redaction_touches_only_the_matched_occurrence(self):
        """An identical value elsewhere in the document must not be redacted."""
        import json

        from sessionfs.security.secrets import scan_dlp
        from sessionfs.server.dlp import redact_and_repack

        policy = [{
            "name": "employee_code",
            "regex": r'"employee_code"\s*:\s*"[^"]{8,}"',
            "category": "secret",
            "severity": "high",
        }]
        line = json.dumps({"safe": "SAMEVALUE12345", "employee_code": "SAMEVALUE12345"})
        findings = scan_dlp(line, categories=["secrets"], custom_patterns=policy)
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, findings, categories=["secrets"], custom_patterns=policy)
        record = json.loads(self._read_messages(repacked).decode("utf-8").splitlines()[0])
        assert record["safe"] == "SAMEVALUE12345"
        assert "SAMEVALUE12345" not in json.dumps({k: v for k, v in record.items() if k != "safe"})

    def test_marker_shaped_text_is_not_trusted(self):
        """Pushed text that merely looks like a redaction marker must be
        scanned like anything else, or a secret could hide inside one."""
        import json

        from sessionfs.security.secrets import scan_dlp
        from sessionfs.server.dlp import decoded_json_text, redact_and_repack

        for content in (
            '[REDACTED:password = "supersecret123"]',
            "[REDACTED:sk-proj-abcdefghijklmnopqrstuvwxyz123456]",
        ):
            line = json.dumps({"content": content})
            findings = scan_dlp(line + "\n" + decoded_json_text(line), categories=["secrets"])
            archive = self._archive_with_messages([line])
            repacked = redact_and_repack(archive, findings, categories=["secrets"])
            out = self._read_messages(repacked).decode("utf-8")
            assert "supersecret123" not in out, content
            assert "abcdefghijklmnopqrstuvwxyz123456" not in out, content

    def test_genuine_markers_from_custom_patterns_are_left_alone(self):
        import json

        from sessionfs.server.dlp import redact_and_repack

        policy = [{"name": "employee_code", "regex": r"EMP-\d{5}", "category": "secret", "severity": "high"}]
        line = json.dumps({"content": 'password = "[REDACTED:employee_code]"'})
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, [], categories=["secrets"], custom_patterns=policy)
        assert self._read_messages(repacked).decode("utf-8").splitlines()[0] == line

    def test_redaction_preserves_numbers_exactly(self):
        """Rewriting a redacted line must not round decimals or turn huge
        exponents into Infinity (not valid JSON)."""
        import json

        from sessionfs.server.dlp import redact_and_repack

        line = (
            '{"large":1e9999,"precise":3.14159265358979323846264338327950288,'
            '"big":' + "9" * 5000 + ',"neg":-0.0,"password":"x","note":"password = \\"supersecretvalue123\\""}'
        )
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, [], categories=["secrets"])
        out = self._read_messages(repacked).decode("utf-8").splitlines()[0]
        assert "supersecretvalue123" not in out
        assert '"large":1e9999' in out
        assert '"precise":3.14159265358979323846264338327950288' in out
        assert '"big":' + "9" * 5000 in out
        assert '"neg":-0.0' in out

        def strict(constant: str) -> None:
            raise AssertionError(f"non-standard JSON constant {constant}")

        import sys as _sys

        old_limit = _sys.get_int_max_str_digits() if hasattr(_sys, "get_int_max_str_digits") else None
        if old_limit is not None:
            _sys.set_int_max_str_digits(0)
        try:
            json.loads(out, parse_constant=strict)
        finally:
            if old_limit is not None:
                _sys.set_int_max_str_digits(old_limit)

    def test_a_custom_rule_named_like_a_secret_never_puts_it_in_a_marker(self):
        """A custom pattern whose NAME is itself a secret must not have that
        name written into markers (later scans trust and skip markers)."""
        import json

        from sessionfs.server.dlp import custom_marker_labels, redact_and_repack

        secret_name = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
        policy = [{"name": secret_name, "regex": r"EMP-\d{5}", "category": "secret", "severity": "high"}]
        assert custom_marker_labels(policy) == []
        line = json.dumps({"content": "badge EMP-12345 issued"})
        archive = self._archive_with_messages([line])
        repacked = redact_and_repack(archive, [], categories=["secrets"], custom_patterns=policy)
        out = self._read_messages(repacked).decode("utf-8")
        assert "EMP-12345" not in out
        assert secret_name not in out
        assert "[REDACTED:custom_pattern]" in out

    def test_nested_tool_use_and_tool_result_redacted(self):
        import json

        from sessionfs.server.dlp import redact_and_repack

        tool_use = {
            "msg_id": "m3",
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "tool_name": "bash",
                "tool_use_id": "tu1",
                "input": {"command": "export DATABASE_URL=postgresql://user:pass@db.example.com:5432/app"},
            }],
        }
        tool_result = {
            "msg_id": "m4",
            "role": "tool",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "tu1",
                "content": "connected to postgresql://user:pass@db.example.com:5432/app",
            }],
        }
        lines = [json.dumps(tool_use), json.dumps(tool_result)]
        archive = self._archive_with_messages(lines)

        findings = scan_dlp("\n".join(lines), categories=["secrets"])
        assert any(f.pattern_name == "database_url" for f in findings)

        repacked = redact_and_repack(archive, findings)
        repacked_text = self._read_messages(repacked).decode("utf-8")
        assert "postgresql://user:pass@db.example.com:5432/app" not in repacked_text
        assert repacked_text.count("[REDACTED:database_url]") == 2

    def test_unchanged_lines_are_byte_identical(self):
        import json

        from sessionfs.server.dlp import redact_and_repack

        clean = '{"a":  1,  "b": 2}'  # unusual spacing must be preserved verbatim
        secret = json.dumps({
            "msg_id": "m5",
            "role": "assistant",
            "content": [{
                "type": "text",
                "text": 'database_url = "postgresql://user:pass@db.example.com:5432/app"',
            }],
        })
        archive = self._archive_with_messages([clean, secret])

        findings = scan_dlp(clean + "\n" + secret, categories=["secrets"])
        repacked = redact_and_repack(archive, findings)
        repacked_lines = self._read_messages(repacked).decode("utf-8").splitlines()

        # The clean line must survive byte-for-byte (no re-serialization).
        assert clean in repacked_lines
        assert "[REDACTED:database_url]" in "\n".join(repacked_lines)

    def test_malformed_line_redacted_via_raw_fallback(self):
        from sessionfs.server.dlp import redact_and_repack

        malformed = 'password = "supersecretvalue123"'  # NOT valid JSON
        archive = self._archive_with_messages([malformed])

        findings = scan_dlp(malformed, categories=["secrets"])
        assert any(f.pattern_name == "generic_password_assignment" for f in findings)

        repacked = redact_and_repack(archive, findings)
        repacked_text = self._read_messages(repacked).decode("utf-8")
        assert "supersecretvalue123" not in repacked_text
        assert "[REDACTED:generic_password_assignment]" in repacked_text
