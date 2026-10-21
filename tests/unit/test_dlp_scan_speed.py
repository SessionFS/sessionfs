"""GH #71: large-session DLP scans and redaction are fast AND unchanged.

The scanner skips lines and positions a pattern provably can't match, and
redaction skips JSONL lines it provably leaves byte-identical. These tests
pin both halves: results are compared with a straightforward every-line /
every-value reference on randomized and adversarial inputs, and the time a
push's CPU work takes is checked to grow linearly with session size and to
stay flat as the number of secrets grows (the old cost was values x secret
spellings).
"""

from __future__ import annotations

import base64
import io
import json
import random
import re
import subprocess
import sys
import tarfile
import time

import pytest

from sessionfs.security import secrets
from sessionfs.security.secrets import (
    PHI_PATTERNS,
    SECRET_PATTERNS,
    SEVERITY_MAP,
    DLPFinding,
    scan_dlp,
    scan_dlp_builtin,
)
from sessionfs.server import dlp
from tests.utils.synthetic_session import build_session

# ---------------------------------------------------------------------------
# Reference scanner: every pattern, every position, every line (the scanner
# as it was before line planning).
# ---------------------------------------------------------------------------


def _reference_scan(
    text: str,
    categories: list[str] | None = None,
    custom_patterns: list[dict] | None = None,
    allowlist: list[str] | None = None,
) -> list[DLPFinding]:
    if categories is None:
        categories = ["secrets", "phi"]
    findings: list[DLPFinding] = []
    custom = [
        (cp["name"], re.compile(cp["regex"]), cp["category"], cp["severity"])
        for cp in custom_patterns or []
    ]
    for line_idx, line in enumerate(text.splitlines(), start=1):
        if "secrets" in categories:
            for name, pattern in SECRET_PATTERNS.items():
                for m in pattern.finditer(line):
                    if secrets._is_dlp_allowlisted(m.group(0), allowlist):
                        continue
                    findings.append(DLPFinding(name, "secret", SEVERITY_MAP.get(name, "medium"),
                                               line_idx, m.group(0), secrets._build_context(line, m)))
        if "phi" in categories:
            for name, (pattern, severity) in PHI_PATTERNS.items():
                for m in pattern.finditer(line):
                    if secrets._is_dlp_allowlisted(m.group(0), allowlist):
                        continue
                    findings.append(DLPFinding(name, "phi", severity, line_idx, m.group(0),
                                               secrets._build_context(line, m)))
        for name, regex, category, severity in custom:
            for m in regex.finditer(line):
                if secrets._is_dlp_allowlisted(m.group(0), allowlist):
                    continue
                findings.append(DLPFinding(name, category, severity, line_idx, m.group(0),
                                           secrets._build_context(line, m)))
    return findings


def _reference_builtin(text: str, categories: list[str] | None = None) -> list[DLPFinding]:
    if categories is None:
        categories = ["secrets"]
    findings: list[DLPFinding] = []
    for line_idx, line in enumerate(text.splitlines(), start=1):
        if "secrets" in categories:
            for name, pattern in SECRET_PATTERNS.items():
                for m in pattern.finditer(line):
                    if secrets._is_allowlisted(m.group(0)):
                        continue
                    ctx = line[max(0, m.start() - 25):min(len(line), m.end() + 25)]
                    findings.append(DLPFinding(name, "secret", SEVERITY_MAP.get(name, "medium"),
                                               line_idx, m.group(0), ctx))
        if "phi" in categories:
            for name, (pattern, severity) in PHI_PATTERNS.items():
                for m in pattern.finditer(line):
                    ctx = line[max(0, m.start() - 25):min(len(line), m.end() + 25)]
                    findings.append(DLPFinding(name, "phi", severity, line_idx, m.group(0), ctx))
    return findings


# ---------------------------------------------------------------------------
# Facts the shortcuts rely on
# ---------------------------------------------------------------------------


def test_case_traps_are_every_non_ascii_char_matching_an_ascii_letter() -> None:
    letter = re.compile("[a-z]", re.IGNORECASE)
    found = {chr(c) for c in range(128, sys.maxunicode + 1) if letter.match(chr(c))}
    assert found == set(secrets._CASE_TRAPS)


def test_only_a_case_trap_grows_when_lowercased() -> None:
    grows = {chr(c) for c in range(sys.maxunicode + 1) if len(chr(c).lower()) != 1}
    assert grows <= set(secrets._CASE_TRAPS)


def test_every_rule_names_a_builtin_pattern_and_has_needles() -> None:
    names = set(SECRET_PATTERNS) | set(PHI_PATTERNS)
    assert set(secrets._SCAN_RULES) <= names
    breaks = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
    for name, rule in secrets._SCAN_RULES.items():
        assert rule.needles, name
        literals = list(rule.needles) + [lit for lit, _ in rule.anchors or ()]
        for literal in literals:
            assert literal and not any(b in literal for b in breaks), (name, literal)
        if rule.needles_folded:
            assert all(n == n.lower() for n in rule.needles), name
        if rule.anchors_folded:
            assert all(lit == lit.lower() for lit, _ in rule.anchors or ()), name


def test_line_starts_match_splitlines() -> None:
    rng = random.Random(3)
    pieces = ["a", "bc", "\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x85", "\u2028", "\u2029", "xyz", ""]
    for _ in range(5000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 14)))
        lines = text.splitlines()
        starts = secrets.line_starts(text)
        assert len(starts) == len(lines)
        for start, line in zip(starts, lines):
            assert text[start:start + len(line)] == line


# ---------------------------------------------------------------------------
# Scanner equivalence
# ---------------------------------------------------------------------------

def _literals() -> list[str]:
    out: set[str] = set()
    for rule in secrets._SCAN_RULES.values():
        out.update(rule.needles)
        out.update(lit for lit, _ in rule.anchors or ())
    return sorted(out)


_FRAGMENTS = _literals() + [
    "Authorization", "Bearer ", "auth_token", "access_token", "AWS_SECRET_ACCESS_KEY",
    "postgresql://", "mongodb+srv://", "redis://u:p@h", "date of birth", "health plan",
    "member id", "patient name", "pt:", "emergency phone", "serial #", "DEA #",
    "MRN#", "DOB:", "fax:", "VIN:", "age 95", "aged 101", "https://x/patient/1",
    " ", " = ", ":", ": ", "=", '"', "'", "`", "\\", "\\\"", "#", "@", "/", ".", "-", "_",
    "123", "45", "6789", "123-45-6789", "(555) 123-4567", "1234567890", "x" * 3,
    "AKIA", "AKIAABCDEFGHIJKLMNOP", "sk-proj-", "sk-ant-", "ghp_", "github_pat_",
    "sk_live_", "rk_test_", "xoxb-", "SG.", "SK" + "0123456789abcdef" * 2,  # split so push protection does not read it as a key
    "eyJhbGciOiJIUzI1NiJ9", ".eyJzdWIiOiIxIn0", "-----BEGIN RSA PRIVATE KEY-----",
    '"type": "service_account"', "AccountKey=", "changeme", "example", "YOUR_KEY_HERE",
    "\u0130", "\u0131", "\u017f", "\u212a", "\xe9", "\u4e2d", "\U0001F600",
    "\n", "\r\n", "\r", "\x0b", "\u2028", "\x85",
]


def _token(rng: random.Random) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=_-."
    return "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 60)))


def _case(rng: random.Random, text: str) -> str:
    mode = rng.random()
    if mode < 0.5:
        return text
    if mode < 0.7:
        return text.upper()
    if mode < 0.8:
        return text.lower()
    return "".join(c.upper() if rng.random() < 0.5 else c.lower() for c in text)


def _random_text(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(1, 40)):
        if rng.random() < 0.3:
            parts.append(_token(rng))
        else:
            parts.append(_case(rng, rng.choice(_FRAGMENTS)))
    return "".join(parts)


_CUSTOM = [
    {"name": "acme_id", "regex": r"ACME-[0-9]{6}", "category": "secret", "severity": "high"},
    {"name": "line_start", "regex": r"^token\b", "category": "secret", "severity": "low"},
]


@pytest.mark.parametrize("seed", range(8))
def test_scanner_matches_every_line_reference(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(300):
        text = _random_text(rng)
        for categories in (["secrets"], ["phi"], ["secrets", "phi"]):
            assert scan_dlp(text, categories) == _reference_scan(text, categories), text
            assert scan_dlp_builtin(text, categories) == _reference_builtin(text, categories), text
        assert scan_dlp(text, None, _CUSTOM, ["sk-proj-AB"]) == _reference_scan(
            text, None, _CUSTOM, ["sk-proj-AB"]
        ), text


@pytest.mark.parametrize(
    "text",
    [
        "auth_token = 'abcdefghij'\nAUTH_TOKEN: \"abcdefghij\"",
        "pa\u017fsword = 'abcdefgh123'",  # long s matches "s" under IGNORECASE
        "\u212aey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # Kelvin sign
        "\u0130 token = 'abcdefghijk' then password: \"abcdefghijk\"",
        "token=\"aaaaaaaa\" token=\"bbbbbbbb\" passwordtoken=\"cccccccc\"",
        "123-45-6789\r\n-12-34-5678 999-99-9999-99",
        "x\u2028api_key: 'zzzzzzzzzz'\x85Bearer: Bearer abcdefghijabcdefghijabcdefghij",
        "postgres://u:p@h mysql://a:b@c redis://x:y@z",
        "AKIAABCDEFGHIJKLMNOP AKIAABCDEFGHIJKLMNOPQ",
    ],
)
def test_scanner_edge_cases_match_reference(text: str) -> None:
    for categories in (["secrets"], ["secrets", "phi"]):
        assert scan_dlp(text, categories) == _reference_scan(text, categories)
        assert scan_dlp_builtin(text, categories) == _reference_builtin(text, categories)


# ---------------------------------------------------------------------------
# Redaction equivalence: skipping provably-unchanged lines changes nothing
# ---------------------------------------------------------------------------

_KEYS = ["password", "token", "api_key", "text", "content", "input", "AccountKey", "n", "auth"]
_MARKERS = ["[REDACTED:generic_password_assignment]", "[REDACTED:residual]", "[REDACTED:acme_id]"]


def _random_value(rng: random.Random, depth: int = 0) -> object:
    roll = rng.random()
    if depth < 4 and roll < 0.15:
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    if depth < 4 and roll < 0.3:
        return {
            (_case(rng, rng.choice(_KEYS)) if rng.random() < 0.7 else _random_text(rng)) + str(i):
            _random_value(rng, depth + 1)
            for i in range(rng.randint(0, 4))
        }
    if roll < 0.4:
        return rng.randint(-10**12, 10**12)
    if roll < 0.45:
        return rng.choice([True, False, None])
    if roll < 0.5:
        return rng.choice(_MARKERS)
    return _random_text(rng)


def _random_jsonl(rng: random.Random) -> bytes:
    lines = []
    for _ in range(rng.randint(1, 25)):
        roll = rng.random()
        if roll < 0.08:
            lines.append(_random_text(rng).replace("\n", " "))  # malformed line
        elif roll < 0.12:
            lines.append("")
        else:
            lines.append(json.dumps(_random_value(rng), ensure_ascii=rng.random() < 0.5))
    return ("\n".join(lines) + "\n").encode()


def _archive(messages: bytes) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in (("messages.jsonl", messages), ("manifest.json", b'{"title": "t"}')):
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def _members(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}


def _findings(messages: bytes, policy: dict) -> list[DLPFinding]:
    raw = messages.decode("utf-8", errors="replace")
    try:
        decoded = dlp.decoded_json_text(raw)
    except dlp.DlpUninspectableError:
        decoded = ""
    text = dlp.mask_redaction_markers(raw + "\n" + decoded, policy.get("custom_patterns"))
    return dlp.dedupe_findings(scan_dlp(text, **policy))


def _redact(archive: bytes, findings: list[DLPFinding], policy: dict) -> object:
    try:
        return _members(dlp.redact_and_repack(archive, findings, member_limit_bytes=10**8, **policy))
    except Exception as exc:  # the same error must be raised either way
        return (type(exc).__name__, str(exc))


_POLICIES = [
    {"categories": ["secrets"], "custom_patterns": None, "allowlist": None},
    {"categories": ["secrets", "phi"], "custom_patterns": None, "allowlist": None},
    {"categories": ["secrets"], "custom_patterns": _CUSTOM, "allowlist": ["changeme"]},
]


@pytest.mark.parametrize("seed", range(6))
def test_redaction_is_identical_to_the_full_value_level_path(seed: int, monkeypatch) -> None:
    rng = random.Random(1000 + seed)
    cases = []
    for _ in range(40):
        messages = _random_jsonl(rng)
        archive = _archive(messages)
        for policy in _POLICIES:
            findings = _findings(messages, policy)
            cases.append((archive, findings, policy, _redact(archive, findings, policy)))
    monkeypatch.setattr(dlp, "_unchanged_lines", lambda *args, **kwargs: set())
    for archive, findings, policy, fast in cases:
        assert _redact(archive, findings, policy) == fast


def test_redaction_of_a_synthetic_session_is_identical_to_the_full_path(monkeypatch) -> None:
    archive = build_session("ses_eq71", scale=0.05)
    messages = _members(archive)["messages.jsonl"]
    policy = _POLICIES[0]
    findings = _findings(messages, policy)
    assert findings
    fast = _redact(archive, findings, policy)
    assert fast != _members(archive)  # something was redacted
    monkeypatch.setattr(dlp, "_unchanged_lines", lambda *args, **kwargs: set())
    assert _redact(archive, findings, policy) == fast


def test_too_deep_line_still_refused() -> None:
    deep = ('{"a":' + "[" * 300 + '"x"' + "]" * 300 + "}\n").encode()
    secret_line = json.dumps({"text": 'password = "supersecretvalue123"'}).encode() + b"\n"
    archive = _archive(secret_line + deep)
    findings = _findings(secret_line, _POLICIES[0])
    with pytest.raises(dlp.DlpUninspectableError):
        dlp.redact_and_repack(archive, findings, member_limit_bytes=10**8, **_POLICIES[0])


# ---------------------------------------------------------------------------
# Timing. CPU time measured in a fresh interpreter (see
# tests/unit/test_instruction_scan.py: late in a suite run the test process
# itself skews large runs). Ratios carry the regression check; absolute
# limits have wide headroom for slow CI runners. Strict wall-clock budgets
# are marked `perf` (excluded by default).
# ---------------------------------------------------------------------------

_PUSH_CPU_TIMER = """
import base64, json, sys, time
from sessionfs.server.routes.sessions import _prepare_sync_archive
from sessionfs.server.tiers import Tier
from sessionfs.server.timing import PhaseTimer
job = json.load(sys.stdin)
policy = {"enabled": True, "mode": "redact", "categories": ["secrets"]}
def run(archive):
    _prepare_sync_archive(archive, session_id="ses_perf", effective_tier=Tier.ENTERPRISE,
                          dlp_policy=policy, timer=PhaseTimer())
def best(archive, count=3):
    run(archive)  # warm the regex cache
    times = []
    for _ in range(count):
        started = time.process_time()
        run(archive)
        times.append(time.process_time() - started)
    return min(times)
print(json.dumps([best(base64.b64decode(a)) for a in job]))
"""


def _push_cpu_times(*archives: bytes) -> list[float]:
    job = json.dumps([base64.b64encode(a).decode() for a in archives])
    done = subprocess.run(
        [sys.executable, "-c", _PUSH_CPU_TIMER],
        input=job, capture_output=True, text=True, timeout=600, check=True,
    )
    return json.loads(done.stdout)


def test_push_cpu_time_grows_linearly_with_session_size() -> None:
    small = build_session("ses_perf_s", scale=1 / 32)
    large = build_session("ses_perf_l", scale=1 / 8)
    t_small, t_large = _push_cpu_times(small, large)
    # 4x the content and 4x the secrets: linear is ~4x (typical 3.9x).
    assert t_large < 7 * t_small, (t_small, t_large)
    # Typical ~0.6 s for 1/8 of the GH #71 shape on a dev machine (~5 s
    # before the fix).
    assert t_large < 15, t_large


def test_push_cpu_time_barely_grows_with_the_number_of_secrets() -> None:
    few = build_session("ses_perf_f", scale=1 / 16, secret_scale=1 / 8)
    many = build_session("ses_perf_m", scale=1 / 16, secret_scale=4)
    t_few, t_many = _push_cpu_times(few, many)
    # 32x the secrets on the same content: typical ~2x (only lines holding a
    # secret take the full redaction path). Before the fix every value was
    # searched for every secret spelling and this was ~5x.
    assert t_many < 3.5 * t_few, (t_few, t_many)


@pytest.mark.perf
def test_full_size_gh71_session_processes_in_seconds() -> None:
    from sessionfs.server.routes.sessions import _prepare_sync_archive
    from sessionfs.server.tiers import Tier
    from sessionfs.server.timing import PhaseTimer

    archive = build_session("ses_perf_full", scale=1.0)
    started = time.perf_counter()
    for mode in ("warn", "redact"):
        timer = PhaseTimer()
        _prepare_sync_archive(
            archive,
            session_id="ses_perf_full",
            effective_tier=Tier.ENTERPRISE,
            dlp_policy={"enabled": True, "mode": mode, "categories": ["secrets"]},
            timer=timer,
        )
    # Both modes together; ~8 s on a dev machine (was ~135 s).
    assert time.perf_counter() - started < 20
