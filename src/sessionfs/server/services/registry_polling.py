"""Server-side polling of registered Git marketplace registries: the
security-sensitive building blocks (GitHub #69, migration 079).

The server fetches a customer-supplied Git URL, so everything here is
written fail-closed (threat model: docs/design/git-registry-polling.md):

- Tag patterns use a RESTRICTED syntax matched by a linear-time NFA
  simulation (no groups, no alternation, no backreferences), so a
  pathological pattern cannot cause catastrophic backtracking.
- Network: https only. The host is resolved HERE, every address is checked
  (loopback, link-local, metadata, multicast, reserved and every non-global
  range are refused; private ranges only via the operator's explicit
  ``SFS_REGISTRY_POLL_ALLOWED_PRIVATE_CIDRS`` opt-in), and git is then
  PINNED to the checked addresses with ``http.curloptResolve`` so it never
  re-resolves (no DNS-rebinding window). Redirects are disabled
  (``http.followRedirects=false``), so no request can land on another host.
- git runs with a fully replaced environment: no system or global config
  (``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL=/dev/null``, ``HOME`` in the
  sandbox), no proxy / trace / askpass variables, no terminal prompt, only
  the https protocol (``GIT_ALLOW_PROTOCOL=https``), hooks disabled, no
  submodules, no LFS. The repository is bare and never checked out.
- Credentials are a REFERENCE (``env:SFS_REGISTRY_CRED_<NAME>`` or
  ``k8s:<name>``), resolved at poll time, bound to the registry's org (and
  optionally to hosts) by the operator, and handed to git as an
  ``http.<url>.extraHeader`` through ``GIT_CONFIG_*`` environment variables:
  never in the URL, argv, logs, stored rows or error text.
"""

from __future__ import annotations

import base64
import ipaddress
import os
import re
import socket
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from sessionfs.skills.models import SkillError
from sessionfs.skills.sources import (
    GitTransportError,
    _require_git_version,
    canonical_git_host,
    _run_git,
    materialize_git_tree,
    normalize_git_url,
)

# ---------------------------------------------------------------------------
# Settings bounds
# ---------------------------------------------------------------------------

DEFAULT_POLL_INTERVAL_SECONDS = 900
MIN_POLL_INTERVAL_SECONDS = 300
MAX_POLL_INTERVAL_SECONDS = 7 * 24 * 3600
MAX_BACKOFF_SECONDS = 24 * 3600
DEFAULT_TAG_PATTERN = r"v?\d+\.\d+\.\d+"
MAX_TAG_PATTERN_LENGTH = 200
_MAX_PATTERN_ATOMS = 64
_MAX_REPEAT_BOUND = 64
MAX_REF_NAME_LENGTH = 200
# Remote ref listing bounds: a hostile server cannot make the poller buffer
# an unbounded ref advertisement.
MAX_REMOTE_REFS = 20_000
_MAX_LS_REMOTE_LINE = 1024
# Packages taken from one ref (marketplace.json plugins or the repo root).
MAX_PACKAGES_PER_REF = 50
# New refs processed in one poll of one registry (oldest backlog is skipped).
MAX_NEW_REFS_PER_POLL = 5
DEFAULT_POLL_TIMEOUT_SECONDS = 300
# One scheduler run (POST /admin/skills/registries/poll) claims no new
# registry once elapsed + per-registry timeout + publish slack would exceed
# this budget. Defaults: 300 s fetch budget + 300 s publish slack, 1200 s run
# budget, so a run starts registries during its first 600 s and ends by
# about 1200 s; the Helm CronJob allows budget + 120 s (curl) / + 180 s (job)
# and the hosted scheduler's attempt deadline must be >= budget + 120 s.
DEFAULT_RUN_BUDGET_SECONDS = 1200
PUBLISH_SLACK_SECONDS = 300
DEFAULT_MAX_POLLING_REGISTRIES_PER_ORG = 10
DEFAULT_MAX_PER_ORG_PER_RUN = 2

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Conservative git ref-name subset for tags and branches the poller will
# fetch (passed to git as one argv element after `--`).
_REF_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}$")


def valid_ref_name(name: str) -> bool:
    return (
        bool(_REF_NAME_RE.fullmatch(name))
        and ".." not in name
        and "//" not in name
        and "@{" not in name
        and not name.endswith((".lock", "/", "."))
        and "/." not in name
    )


def validate_branch(value: str) -> str:
    value = value.strip()
    if value.startswith("refs/heads/"):
        value = value[len("refs/heads/"):]
    if not valid_ref_name(value):
        raise ValueError("branch must be a simple Git branch name")
    return value


# ---------------------------------------------------------------------------
# Restricted tag pattern (linear time)
# ---------------------------------------------------------------------------

_META = set(".^$*+?()[]{}|\\")
_DIGITS = (("0", "9"),)
_WORD = (("0", "9"), ("A", "Z"), ("a", "z"), ("_", "_"))


@dataclass(frozen=True)
class _Atom:
    kind: str  # "lit" | "any" | "set"
    char: str = ""
    ranges: tuple[tuple[str, str], ...] = ()
    negated: bool = False
    lo: int = 1
    hi: int | None = 1

    def matches(self, ch: str) -> bool:
        if self.kind == "any":
            return True
        if self.kind == "lit":
            return ch == self.char
        hit = any(low <= ch <= high for low, high in self.ranges)
        return hit != self.negated


class TagPattern:
    """A compiled restricted pattern; ``fullmatch`` runs in
    O(len(name) * states) with no backtracking."""

    def __init__(self, source: str, atoms: list[_Atom]):
        self.source = source
        self._atoms = atoms

    def _closure(self, states: set[tuple[int, int]]) -> set[tuple[int, int]]:
        stack = list(states)
        seen = set(states)
        while stack:
            index, count = stack.pop()
            if index < len(self._atoms) and count >= self._atoms[index].lo:
                nxt = (index + 1, 0)
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    def fullmatch(self, name: str) -> bool:
        if len(name) > MAX_REF_NAME_LENGTH:
            return False
        states = self._closure({(0, 0)})
        for ch in name:
            stepped: set[tuple[int, int]] = set()
            for index, count in states:
                if index >= len(self._atoms):
                    continue
                atom = self._atoms[index]
                if (atom.hi is None or count < atom.hi) and atom.matches(ch):
                    cap = atom.lo if atom.hi is None else atom.hi
                    stepped.add((index, min(count + 1, cap)))
            if not stepped:
                return False
            states = self._closure(stepped)
        return (len(self._atoms), 0) in states


def _escape_ranges(ch: str) -> tuple[tuple[str, str], ...] | None:
    if ch == "d":
        return _DIGITS
    if ch == "w":
        return _WORD
    return None


def compile_tag_pattern(pattern: str | None) -> TagPattern:
    """Compile a restricted tag pattern, or raise ValueError.

    Supported: literal characters, ``.``, escapes ``\\d`` ``\\w`` and escaped
    punctuation (``\\.`` ``\\-`` ...), classes ``[...]`` / ``[^...]`` with
    ranges, quantifiers ``? * + {n} {n,} {n,m}`` (bounds <= 64), and an
    optional leading ``^`` / trailing ``$`` (the whole tag name always has to
    match). Refused: groups, alternation, backreferences, lookarounds, lazy
    or possessive quantifiers, and anything else."""
    source = DEFAULT_TAG_PATTERN if pattern is None else pattern
    if not isinstance(source, str) or not source or len(source) > MAX_TAG_PATTERN_LENGTH:
        raise ValueError(f"tag_pattern must be 1-{MAX_TAG_PATTERN_LENGTH} characters")
    if any(ord(ch) < 32 or ord(ch) > 126 for ch in source):
        raise ValueError("tag_pattern must be printable ASCII")
    body = source
    if body.startswith("^"):
        body = body[1:]
    if body.endswith("$") and not body.endswith("\\$"):
        body = body[:-1]
    atoms: list[_Atom] = []
    i = 0

    def fail(message: str) -> ValueError:
        return ValueError(f"tag_pattern is not supported: {message}")

    while i < len(body):
        ch = body[i]
        atom: _Atom
        if ch == "\\":
            if i + 1 >= len(body):
                raise fail("trailing backslash")
            esc = body[i + 1]
            ranges = _escape_ranges(esc)
            if ranges is not None:
                atom = _Atom("set", ranges=ranges)
            elif esc.isalnum():
                raise fail(f"escape \\{esc} (only \\d, \\w and escaped punctuation)")
            else:
                atom = _Atom("lit", char=esc)
            i += 2
        elif ch == ".":
            atom = _Atom("any")
            i += 1
        elif ch == "[":
            j = i + 1
            negated = False
            if j < len(body) and body[j] == "^":
                negated = True
                j += 1
            ranges_list: list[tuple[str, str]] = []
            while j < len(body) and body[j] != "]":
                c = body[j]
                if c == "\\":
                    if j + 1 >= len(body):
                        raise fail("unterminated class")
                    esc = body[j + 1]
                    esc_ranges = _escape_ranges(esc)
                    if esc_ranges is not None:
                        ranges_list.extend(esc_ranges)
                        j += 2
                        continue
                    if esc.isalnum():
                        raise fail(f"escape \\{esc} in a class")
                    c = esc
                    j += 1
                elif c == "[":
                    raise fail("nested class")
                if j + 2 < len(body) and body[j + 1] == "-" and body[j + 2] != "]":
                    high = body[j + 2]
                    if high == "\\":
                        raise fail("escaped range end")
                    if high < c:
                        raise fail("reversed range")
                    ranges_list.append((c, high))
                    j += 3
                else:
                    ranges_list.append((c, c))
                    j += 1
            if j >= len(body) or not ranges_list:
                raise fail("empty or unterminated class")
            atom = _Atom("set", ranges=tuple(ranges_list), negated=negated)
            i = j + 1
        elif ch in _META:
            raise fail(f"'{ch}' (groups, alternation and anchors inside the pattern are not allowed)")
        else:
            atom = _Atom("lit", char=ch)
            i += 1
        lo, hi = 1, 1
        if i < len(body) and body[i] in "?*+{":
            q = body[i]
            if q == "?":
                lo, hi = 0, 1
                i += 1
            elif q == "*":
                lo, hi = 0, None
                i += 1
            elif q == "+":
                lo, hi = 1, None
                i += 1
            else:
                end = body.find("}", i)
                if end == -1:
                    raise fail("unterminated {")
                spec = body[i + 1:end]
                match = re.fullmatch(r"(\d{1,2})(,(\d{0,2}))?", spec)
                if match is None:
                    raise fail("bad {} quantifier")
                lo = int(match.group(1))
                if match.group(2) is None:
                    hi = lo
                elif match.group(3) == "":
                    hi = None
                else:
                    hi = int(match.group(3))
                if lo > _MAX_REPEAT_BOUND or (hi is not None and (hi > _MAX_REPEAT_BOUND or hi < lo or hi == 0)):
                    raise fail("{} bounds must be 0-64 and ordered")
                i = end + 1
            if i < len(body) and body[i] in "?*+{":
                raise fail("stacked, lazy or possessive quantifiers")
        atoms.append(_Atom(atom.kind, atom.char, atom.ranges, atom.negated, lo, hi))
        if len(atoms) > _MAX_PATTERN_ATOMS:
            raise fail("too many elements")
    if not atoms:
        raise fail("empty pattern")
    return TagPattern(source, atoms)


# ---------------------------------------------------------------------------
# Credential references
# ---------------------------------------------------------------------------

_ENV_REF_RE = re.compile(r"^env:(SFS_REGISTRY_CRED_[A-Z0-9_]{1,64})$")
_K8S_REF_RE = re.compile(r"^k8s:([a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)$")
_DEFAULT_SECRETS_DIR = "/var/run/secrets/sessionfs/registry-credentials"
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,1024}$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,128}$")
DEFAULT_CREDENTIAL_USERNAME = "x-access-token"


CREDENTIAL_UNAVAILABLE = "The registry credential reference cannot be used; ask your SessionFS operator to check it."


class CredentialError(Exception):
    """A credential reference that cannot be used (fail closed).

    ``str()`` is ONE generic, user-facing message for every cause, so the API
    never tells an org whether a name exists, is unbound, or belongs to
    another org. ``reason`` is the specific cause, for server-side logs only
    (it never contains the secret)."""

    def __init__(self, reason: str):
        super().__init__(CREDENTIAL_UNAVAILABLE)
        self.reason = reason


@dataclass(frozen=True, repr=False)
class Credential:
    username: str
    token: str

    def __repr__(self) -> str:  # never print the token
        return f"Credential(username={self.username!r}, token=<redacted>)"

    def secrets(self) -> tuple[str, ...]:
        raw = f"{self.username}:{self.token}"
        return (self.token, base64.b64encode(raw.encode()).decode())


def validate_credential_ref(value: str) -> str:
    """Accept ``env:SFS_REGISTRY_CRED_<NAME>`` or ``k8s:<secret-name>``;
    anything else (including what looks like a plaintext token) is refused."""
    value = value.strip()
    if _ENV_REF_RE.fullmatch(value) or _K8S_REF_RE.fullmatch(value):
        return value
    raise ValueError(
        "credential_secret_ref must be env:SFS_REGISTRY_CRED_<NAME> or k8s:<secret-name>; "
        "never send a plaintext credential"
    )


def _secrets_dir() -> Path:
    return Path(os.environ.get("SFS_REGISTRY_SECRETS_DIR", _DEFAULT_SECRETS_DIR))


def _read_secret_file(path: Path, *, limit: int = 4096) -> str | None:
    try:
        if path.is_symlink() and not path.resolve().is_file():
            return None
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return None
    if len(raw) > limit:
        return None
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None


def resolve_credential(ref: str, *, org_id: str, host: str) -> Credential:
    """Resolve ``ref`` for a poll of ``host`` on behalf of ``org_id``.

    The operator binds every credential to ONE org (``<NAME>_ORG`` env var or
    the secret's ``org`` key) AND to the hosts it may be sent to (``_HOSTS`` /
    ``hosts``, comma-separated; required). A credential not bound to the
    registry's org and host is refused, so one org can never reference
    another org's credential (or any other server environment variable), and
    no org can send its own credential anywhere the operator did not intend."""
    env_match = _ENV_REF_RE.fullmatch(ref or "")
    k8s_match = _K8S_REF_RE.fullmatch(ref or "")
    if env_match:
        name = env_match.group(1)
        token = os.environ.get(name, "").strip()
        bound_org = os.environ.get(f"{name}_ORG", "").strip()
        hosts = os.environ.get(f"{name}_HOSTS", "").strip()
        username = os.environ.get(f"{name}_USERNAME", "").strip()
    elif k8s_match:
        base = _secrets_dir() / k8s_match.group(1)
        token = _read_secret_file(base / "token") or ""
        bound_org = _read_secret_file(base / "org") or ""
        hosts = _read_secret_file(base / "hosts") or ""
        username = _read_secret_file(base / "username") or ""
    else:
        raise CredentialError("credential reference is not supported")
    if not token:
        raise CredentialError("credential reference did not resolve to a credential")
    if not bound_org:
        raise CredentialError("credential is not bound to an organization by the operator")
    if bound_org != org_id:
        raise CredentialError("credential is bound to a different organization")
    if not hosts:
        raise CredentialError("credential has no host binding (_HOSTS / hosts is required)")
    allowed: set[str] = set()
    for item in hosts.split(","):
        try:
            allowed.add(canonical_git_host(item))
        except SkillError:
            continue
    if canonical_git_host(host) not in allowed:
        raise CredentialError("credential is not bound to this registry host")
    if not _TOKEN_RE.fullmatch(token):
        raise CredentialError("credential contains unsupported characters")
    username = username or DEFAULT_CREDENTIAL_USERNAME
    if not _USERNAME_RE.fullmatch(username):
        raise CredentialError("credential username contains unsupported characters")
    return Credential(username=username, token=token)


# ---------------------------------------------------------------------------
# Network guard
# ---------------------------------------------------------------------------

_METADATA = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("fd00:ec2::254"),
}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class FetchBlocked(Exception):
    """The SSRF guard refused a registry URL or address."""


def allowed_private_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Operator opt-in (self-hosted): private ranges the poller may reach,
    e.g. an internal GitLab. Loopback, link-local and metadata addresses are
    never reachable, opt-in or not. A malformed value disables the opt-in."""
    raw = os.environ.get("SFS_REGISTRY_POLL_ALLOWED_PRIVATE_CIDRS", "").strip()
    if not raw:
        return []
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            network = ipaddress.ip_network(item, strict=False)
        except ValueError:
            return []
        if network.is_loopback or network.is_link_local or network.prefixlen == 0:
            return []
        networks.append(network)
    return networks


def _embedded_ipv4(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    if not isinstance(ip, ipaddress.IPv6Address):
        return []
    found: list[ipaddress.IPv4Address] = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip.teredo is not None:
        found.extend(ip.teredo)
    if ip in _NAT64:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return found


def address_allowed(
    raw: str,
    allowed: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = None,
) -> bool:
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip, *_embedded_ipv4(ip)]
    for candidate in candidates:
        if candidate in _METADATA:
            return False
        if (
            candidate.is_loopback
            or candidate.is_link_local
            or candidate.is_multicast
            or candidate.is_unspecified
            or candidate.is_reserved
        ):
            return False
    for candidate in candidates:
        if candidate.is_global:
            continue
        if not any(candidate in network for network in (allowed or [])):
            return False
    return True


def resolve_host(host: str, port: int) -> list[str]:
    """Resolve and check every address; raise FetchBlocked unless ALL are
    allowed (one bad address in a round-robin set is enough to refuse)."""
    allowed = allowed_private_networks()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (socket.gaierror, UnicodeError) as exc:
            raise FetchBlocked(f"could not resolve {host}") from exc
        addresses = []
        for _family, _type, _proto, _canon, sockaddr in infos:
            address = str(sockaddr[0])
            if address not in addresses:
                addresses.append(address)
    if not addresses:
        raise FetchBlocked(f"could not resolve {host}")
    for address in addresses:
        if not address_allowed(address, allowed):
            raise FetchBlocked(f"{host} resolves to a disallowed address")
    return addresses[:8]


def check_poll_url(url: str) -> tuple[str, str, int]:
    """Static checks of a registry URL for polling: (normalized, host, port).
    https only; ssh is not polled (see the design note)."""
    try:
        normalized = normalize_git_url(url)
    except SkillError as exc:
        raise FetchBlocked(str(exc)) from exc
    parts = urlsplit(normalized)
    if parts.scheme != "https":
        raise FetchBlocked("server-side polling supports https:// registries only")
    if parts.username or parts.password:
        raise FetchBlocked("registry URL must not embed credentials")
    try:
        host = canonical_git_host(parts.hostname or "")
    except SkillError as exc:
        raise FetchBlocked(str(exc)) from exc
    port = parts.port or 443
    return normalized, host, port


# ---------------------------------------------------------------------------
# Git transport
# ---------------------------------------------------------------------------

_PASS_THROUGH_ENV = ("PATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "GIT_SSL_CAINFO", "GIT_EXEC_PATH", "TMPDIR")


@dataclass(repr=False)
class GitTransport:
    url: str
    env: dict[str, str]
    config: tuple[str, ...]
    secrets: tuple[str, ...] = field(default_factory=tuple)

    def __repr__(self) -> str:
        return f"GitTransport(url={self.url!r})"


def _base_env(sandbox: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in _PASS_THROUGH_ENV if os.environ.get(key)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update(
        {
            "HOME": str(sandbox),
            "XDG_CONFIG_HOME": str(sandbox),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_ALLOW_PROTOCOL": "https",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    return env


def prepare_transport(url: str, credential: Credential | None, sandbox: Path) -> GitTransport:
    """Resolve, check and pin the registry host; build git's environment and
    ``-c`` settings. Raises FetchBlocked."""
    normalized, host, port = check_poll_url(url)
    addresses = resolve_host(host, port)
    pinned = ",".join(f"[{a}]" if ":" in a else a for a in addresses)
    host_for_url = f"[{host}]" if ":" in host else host
    origin = f"https://{host_for_url}" + ("" if port == 443 else f":{port}")
    config = (
        f"http.curloptResolve={host}:{port}:{pinned}",
        "http.followRedirects=false",
        "http.sslVerify=true",
        "protocol.allow=never",
        "protocol.https.allow=always",
        "transfer.credentialsInUrl=die",
        "credential.helper=",
        "submodule.recurse=false",
        "fetch.recurseSubmodules=false",
        "core.fsmonitor=false",
    )
    env = _base_env(sandbox)
    secrets: tuple[str, ...] = ()
    if credential is not None:
        basic = base64.b64encode(f"{credential.username}:{credential.token}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                # URL-scoped: sent only to this registry's origin; redirects off.
                "GIT_CONFIG_KEY_0": f"http.{origin}/.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            }
        )
        secrets = credential.secrets()
    # The registry's own path, unchanged (some hosts, e.g. Azure DevOps
    # `_git/<repo>`, do not accept an added `.git`); scheme, host and port are
    # the checked, pinned ones and the URL carries no userinfo/query/fragment.
    path = urlsplit(url.strip()).path.rstrip("/") or urlsplit(normalized).path
    return GitTransport(url=origin + path, env=env, config=config, secrets=secrets)


@contextmanager
def sandbox_dir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="sfs-registry-poll-") as tmp:
        path = Path(tmp)
        os.chmod(path, 0o700)
        yield path


@dataclass(frozen=True)
class RemoteRef:
    name: str  # short name: tag name or branch name
    ref: str  # full ref: refs/tags/<name> or refs/heads/<name>
    commit: str


def list_remote_refs(
    transport: GitTransport,
    sandbox: Path,
    *,
    branch: str | None = None,
) -> list[RemoteRef]:
    """``git ls-remote`` the tags (or one branch), streamed and bounded.
    Annotated tags resolve to their peeled commit."""
    _require_git_version(sandbox, base_env=transport.env)
    lines = 0
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}

    def on_line(raw: bytes) -> None:
        nonlocal lines
        if not raw:
            return
        lines += 1
        if lines > MAX_REMOTE_REFS:
            raise SkillError("Registry advertises too many refs")
        try:
            text = raw.decode("ascii").rstrip("\r")
            sha, ref = text.split("\t", 1)
        except (UnicodeDecodeError, ValueError):
            return
        if not _SHA_RE.fullmatch(sha):
            return
        if ref.endswith("^{}"):
            peeled[ref[:-3]] = sha
        else:
            direct[ref] = sha

    args = ["ls-remote"]
    if branch is not None:
        args += ["--heads", "--", transport.url, f"refs/heads/{branch}"]
    else:
        args += ["--tags", "--", transport.url]
    _run_git(
        args,
        cwd=sandbox,
        base_env=transport.env,
        extra_config=transport.config,
        on_record=on_line,
        record_separator=b"\n",
        max_record_bytes=_MAX_LS_REMOTE_LINE,
    )
    refs: list[RemoteRef] = []
    prefix = "refs/heads/" if branch is not None else "refs/tags/"
    for ref, sha in direct.items():
        if not ref.startswith(prefix):
            continue
        name = ref[len(prefix):]
        if not valid_ref_name(name):
            continue
        if branch is not None and name != branch:
            continue
        refs.append(RemoteRef(name=name, ref=ref, commit=peeled.get(ref, sha)))
    return refs


def version_sort_key(name: str) -> tuple:
    """Numeric-aware ordering for tag names (v1.10.0 after v1.9.0)."""
    parts = re.findall(r"\d+|[^\d]+", name)
    return tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in parts)


@dataclass
class FetchedPackage:
    name: str
    subdir: str
    data: bytes | None = None
    error_code: str | None = None


def fetch_ref_packages(
    transport: GitTransport,
    ref: RemoteRef,
    *,
    deadline: float,
) -> list[FetchedPackage]:
    """Fetch one ref's tree (bounded, hooks off, no checkout) and pack each
    package it holds: the in-repo plugins of a marketplace manifest, or the
    repository root as one package. External plugin sources are NOT fetched
    (they would let a repository point the server at arbitrary URLs)."""
    from sessionfs.skills.publish_validation import read_publish_source
    from sessionfs.skills.registry import MARKETPLACE_MANIFEST_PATHS, parse_marketplace_full
    from sessionfs.skills.publish_validation import HostedSkillPackageError

    if time.monotonic() > deadline:
        raise GitTransportError("Git source operation timed out")
    with materialize_git_tree(
        transport.url,
        ref.ref,
        expect_commit=ref.commit,
        git_env=transport.env,
        git_config=transport.config,
        deadline=deadline,
    ) as (root, _commit):
        results: list[FetchedPackage] = []
        has_manifest = any((root / item).is_file() for item in MARKETPLACE_MANIFEST_PATHS)
        entries: list[tuple[str, str | None, str | None]] = []  # (name, subdir, error)
        if has_manifest:
            parsed = parse_marketplace_full(root)
            for item in parsed["packages"]:
                if item.get("external_source"):
                    entries.append((item["name"], None, "external_source_not_fetched"))
                else:
                    entries.append((item["name"], item["subdir"], None))
            for item in parsed["unsupported"]:
                entries.append((str(item.get("name", "?"))[:100], None, "unsupported_manifest_entry"))
        else:
            entries.append(("(root)", "", None))
        if len(entries) > MAX_PACKAGES_PER_REF:
            raise SkillError(f"Registry ref lists more than {MAX_PACKAGES_PER_REF} packages")
        for name, subdir, error in entries:
            if error is not None or subdir is None:
                results.append(FetchedPackage(name=name, subdir=subdir or "", error_code=error))
                continue
            if time.monotonic() > deadline:
                raise GitTransportError("Git source operation timed out")
            directory = (root / subdir) if subdir else root
            try:
                directory.resolve().relative_to(root.resolve())
            except ValueError:
                results.append(FetchedPackage(name=name, subdir=subdir, error_code="unsafe_subdir"))
                continue
            if not directory.is_dir() or directory.is_symlink():
                results.append(FetchedPackage(name=name, subdir=subdir, error_code="missing_subdir"))
                continue
            try:
                data, _packed = read_publish_source(directory)
            except HostedSkillPackageError as exc:
                results.append(
                    FetchedPackage(name=name, subdir=subdir, error_code=exc.reason or exc.code)
                )
                continue
            results.append(FetchedPackage(name=name, subdir=subdir, data=data))
        return results


# ---------------------------------------------------------------------------
# Error text
# ---------------------------------------------------------------------------

_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]*@")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization|proxy-authorization)\s*:\s*\S+(\s+\S+)?")
_TOKEN_SHAPES_RE = re.compile(
    r"\b(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|"
    r"sk_sfs_[A-Za-z0-9_-]{8,})\b"
)


def sanitize_summary(text: str, secrets: tuple[str, ...] = (), *, limit: int = 300) -> str:
    """Bounded, credential-free error text for last_poll_error_summary."""
    value = str(text or "")
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    value = _AUTH_HEADER_RE.sub(r"\1: [redacted]", value)
    value = _USERINFO_RE.sub(r"\1", value)
    value = _TOKEN_SHAPES_RE.sub("[redacted]", value)
    value = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in value)
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > limit:
        value = value[: limit - 3] + "..."
    return value


def poll_timeout_seconds() -> int:
    try:
        value = int(os.environ.get("SFS_REGISTRY_POLL_TIMEOUT_SECONDS", str(DEFAULT_POLL_TIMEOUT_SECONDS)))
    except ValueError:
        value = DEFAULT_POLL_TIMEOUT_SECONDS
    return max(30, min(value, 3600))


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return max(low, min(value, high))


def run_budget_seconds() -> int:
    return _int_env("SFS_REGISTRY_POLL_RUN_BUDGET_SECONDS", DEFAULT_RUN_BUDGET_SECONDS, 120, 3000)


def max_polling_registries_per_org() -> int:
    return _int_env("SFS_REGISTRY_POLL_MAX_PER_ORG", DEFAULT_MAX_POLLING_REGISTRIES_PER_ORG, 1, 1000)


def max_per_org_per_run() -> int:
    return _int_env("SFS_REGISTRY_POLL_MAX_PER_ORG_PER_RUN", DEFAULT_MAX_PER_ORG_PER_RUN, 1, 20)


def polling_enabled() -> bool:
    return os.environ.get("SFS_REGISTRY_POLLING_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def backoff_seconds(interval: int, failures: int) -> int:
    base = max(interval, MIN_POLL_INTERVAL_SECONDS)
    return int(min(base * (2 ** max(failures, 1)), MAX_BACKOFF_SECONDS))
