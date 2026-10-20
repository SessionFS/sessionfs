"""Deterministic instruction-pattern scanner for marketplace packages.

Scans the text members of a skill / plugin package (instruction markdown,
frontmatter, hook and MCP configuration, bundled scripts) for patterns seen in
malicious agent skills: fetch-and-run, encoded execution, credential
harvesting, exfiltration sinks, permission bypass and hook auto-approval,
persistence, concealment and defense evasion, hidden HTML-comment
instructions, and invisible / bidirectional Unicode.

Design constraints (marketplace v2 §5.2):

* Deterministic, no LLM, no network. The SAME module runs in
  ``sfs skill validate`` and in the server's publish gate and rescans, so a
  result is reproducible anywhere from the package bytes and the ruleset
  version alone.
* Linear time. Every pattern uses bounded repetition only (no ``*``, ``+``
  or open ``{n,}``; no look-around or back-references; enforced by
  :func:`lint_pattern` in the test suite). Rules that relate two places on a
  line (``curl ... | sh``) anchor on one token and look the other up with a
  binary search over pre-computed token positions, so a pathological input
  never re-scans a window per candidate.
* Findings carry the rule id, file, line, column, a fixed explanation and a
  hash of the flagged line. They NEVER carry the matched text, so a scan
  result can be stored and shown without echoing a secret-like value.

Severity model: ``critical`` refuses publish (and quarantines an already
published version on rescan); ``high`` and ``medium`` are accepted, recorded
on the version and shown; ``low`` is informational.

Shell handling. Matching runs on a DETECTION text that undoes cheap shell
obfuscation (line continuations, ``c\\url``, ``c''url``, ``cu"r"l``,
``$'\\x73h'``) with an offset map back to the physical text. Each statement
holding a fetch or decode command is then parsed by one bounded, linear,
quote-aware shell lexer (skills/shell_lex.py) into command lists, pipelines
and commands - quotes (incl. ANSI-C), escapes, comments in scripts, the
operators ``;`` ``&&`` ``||`` ``|`` ``|&`` ``&``, redirections, here-strings
and nested ``$(...)`` / backticks / ``<(...)`` / ``>(...)``. The rules run on
that structure: fetch-and-run needs a fetch / decode stage and a later
runner in the SAME pipeline (``curl URL && echo ok | sh`` is clean);
substitutions count as an argument or command string of a runner (wrappers
such as sudo / env -S / timeout / su -c / flock -c / script -c are unwrapped,
plus ``VAR=value`` assignments; aliases, functions, variable, brace and
glob command names are over-approximated, see below); download-then-run needs a later
command of the same list to run the download as an argument or via stdin
(``bash < /tmp/x``). In prose a command may follow sentence words or a list /
heading / blockquote / prompt marker. ``exec N< <(curl)`` + ``<&N``,
``tee >(sh)``, here-documents and ``F=curl; $F URL | sh`` are recognized.
A file (1 MiB) or package (4 MiB) packing more shell statements than the
analysis budget is reported ``hidden_content.analysis_budget`` (critical,
fail closed) instead of being analyzed partially. The same budget bounds the
intermediate work of one member: at most MAX_SCAN_ITEMS match positions and
comment spans and MAX_EDITS rewrite edits are collected (line indexes are
block counts, not per-line lists), so a 16 MiB member of repeated tokens
stops early and fails closed in bounded memory.

Command strings a shell or wrapper runs (``bash -c STR``, ``su -c STR``,
``env -S STR`` / ``--split-string=STR``, ``flock`` / ``script -c``, through
``sudo`` / ``doas`` chains, also attached as ``su -c'STR'`` /
``env -S'STR'``) are found by following the real command head only and
re-scanned (depth <= 4, charged to the budget); a finding inside one is
reported once. Shell option arity is followed (``bash --rcfile FILE -c``)
and multicall binaries dispatch to their applet (``busybox sh -c``).
Command strings nested deeper than analyzed, substitutions nested deeper
than the lexer parses, and command strings past the analysis budget fail
closed (``hidden_content.analysis_budget``), never a silent skip.

Shell indirection is deliberately OVER-approximated, not emulated. Earlier
precise emulation of aliases, functions and variables (ordering, scoping,
redefinition, ``unalias`` / ``unset``, ``"$@"`` forwarding) never converged:
each refinement opened a new bypass. Instead, every alias and function name
the member defines ANYWHERE (comments, quoted text and here-documents
included; any case; no ordering, scoping or removal semantics; at most
_DEFINITION_LIMIT definitions per member, past which the member fails
closed) is an UNKNOWN command, as is a command name that is an expansion
(``$F``, ``${F}``), a substitution, a brace expansion (``{ba,}sh``,
``b{a..a}sh`` — never materialized: the expansion is quadratic in memory) or
a glob that could match an interpreter, fetcher or decoder, and ``eval`` /
``iex`` of anything but literal text. A defined name also counts as a runner. An unknown stage that
can feed a runner - or, in a member that fetches or decodes, another unknown
stage - is reported ``fetch_and_run.indirect`` (critical), as is, in such a
member, an unknown command given a runner as an argument (an alias ending in
``|``, a function piping into ``"$@"``), an unknown command whose argument a
later command runs, and a non-literal ``eval``. Literal ``VAR=value``
fetchers keep their direct rules, and an unknowable head with ``-c`` is
analyzed as a shell. Positional / special parameters and indirect
expansion (``$1``, ``$@``, ``${!name}``) are unknown heads in shell code,
eval of a substitution is non-literal, an output process substitution
(``> >(sh)``, ``tee >(sh)``) is a runner sink of every pipeline, and an
attached output option (``-o/tmp/x``, ``--output=/tmp/x``) or redirect links
an unknown download to a later run. Heads are located structurally on the
lexed command (wrapper options, values and assignments followed), and words
and names have no length cap. Members in languages whose own ``function`` /
``$x`` / ``*`` syntax is not shell (JavaScript, Python, HTML ...) are exempt
unless they are shell after all: a shell shebang (``#!/bin/sh``,
``#!/usr/bin/env [-S] bash``), or a member a hook / script / config runs
through a shell. In prose a price (``$5``), Markdown table cells, list
markers, balanced emphasis and an English "function NAME" are not
indirection; fenced blocks marked as shell and indented command lines are
shell code. The quarantines this causes on already
published versions are reviewed by people (marketplace v2 §5.6, CEO
decision D3): a false positive is restored by a moderator, never removed
from devices automatically. In shell code a quoted word may span lines (in
prose a quote closes on its own line). File-descriptor reads are one
budgeted index (MAX_FD_READS) with a streaming pre-check, so only a read
whose command could run something is lexed.

Raw control characters (everything except TAB, LF, CR and a form feed alone
on its line) are refused by policy: ``unicode.control_character`` is
critical in SKILL.md, scripts, hooks and config, high elsewhere. Matching
still strips them under two readings (terminal: escape sequences removed
whole; raw: only the control bytes removed), but the policy - not the
readings - closes concealment through terminal control sequences: in strict
contexts the file is refused whether or not the hidden command can be
reconstructed. A third reading is therefore not used; in other prose a
crafted soup that defeats both readings still carries a high finding.

No fixed windows: padding (spaces, ``>`` markers, blank lines, extra options
or words, long names, quoted / backtick / ANSI-C / here-document spans) can
never move one token out of another's reach. Whitespace runs collapse in the
detection text; keyword pairs (``powershell ... -enc``, ``dig ... $(``,
``rm ... .bash_history`` ...) are matched anywhere on the same logical line;
line-scoped rules use the true logical line; statement continuation, prose
leads, download-then-run steps, ``<&N`` reads, aliases, functions, fetcher
variables and brace expansions have no count or length cap. Every such
search is memoized or cached so the scan stays linear; the analysis budget
above is the only bound, and it fails closed.

Known limits (deterministic static matching cannot follow values computed at
run time; the marketplace does not rely on the scanner alone - consent,
moderation reports, human takedown and scheduled rescans with new rulesets
backstop it):

* command names produced at run time: ``$(printf '\\x63url') URL | sh``,
  ``echo 'hs | ... lruc' | rev | sh``, ``eval "$(echo ... | base64 -d)"`` is
  caught only through the decode rule;
* indirection through files or the environment the scanner cannot see
  (a script fetched by one step and run by an unrelated later step on
  another line, ``source ~/.profile`` after a persistence write);
* a fetch-and-run written inside a quoted string is read as commands
  (``echo 'curl URL | sh' >> ~/.bashrc`` is flagged, by design);
* other interpreters' own obfuscation (Python ``__import__`` tricks, JS
  ``Function`` constructors beyond the listed decode patterns).
"""

from __future__ import annotations

import bisect
import fnmatch
import hashlib
import re
import shlex
import unicodedata
from array import array
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Container, Iterable, Iterator, Sequence
from urllib.parse import urlsplit

from sessionfs.skills.shell_lex import (
    PATTERNS as _SHELL_PATTERNS,
    SUB_MARK,
    Command,
    CommandList,
    LineIndex,
    Pipeline,
    ShellLexer,
    Sub,
    walk_lists,
)

# Bump whenever detection changes (new rules, wider over-approximation): a
# version's latest scan must carry the current ruleset before it can be
# restored from quarantine, and stale scans are re-run (see skill_scanning).
RULESET_VERSION = "2026.10.6"

SEVERITIES = ("low", "medium", "high", "critical")
SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# Storage / display bounds. A package that trips more than this many findings
# is already decided by its worst finding; the result says it was truncated.
MAX_FINDINGS_PER_RULE_PER_FILE = 25
MAX_FINDINGS = 500
MAX_SUMMARY_FINDINGS = 50
# The flagged line is hashed (for suppressions bound to the line) over at most
# this many characters from the start of the line.
_LINE_HASH_CHARS = 8192
# Look-back windows (characters, same line only).
# Longest fetch command eligible for the official-installer downgrade (the
# downgrade only LOWERS a finding, so a longer command keeps full severity).
_PIPE_WINDOW = 400

# Path classification.
_SCRIPT_SUFFIXES = {
    ".sh", ".bash", ".zsh", ".ksh", ".fish", ".py", ".js", ".mjs", ".cjs", ".ts",
    ".ps1", ".psm1", ".bat", ".cmd", ".rb", ".pl", ".php", ".lua",
}
_PROSE_SUFFIXES = {".md", ".markdown", ".mdx", ".txt", ".rst"}
_MARKUP_SUFFIXES = {".md", ".markdown", ".mdx", ".html", ".htm"}
_EXEC_CONFIG_NAMES = {"mcp.json", ".mcp.json", "settings.json", "settings.local.json"}
# Contexts whose content a tool executes or obeys mechanically.
EXEC_CONTEXTS = frozenset({"script", "hook", "config"})


@dataclass(frozen=True)
class Rule:
    rule_id: str
    family: str
    explanation: str


_RULE_LIST = (
    Rule("fetch_and_run.pipe_to_interpreter", "fetch_and_run",
         "Downloads remote content and pipes it straight into a shell or interpreter."),
    Rule("fetch_and_run.process_substitution", "fetch_and_run",
         "Runs a remote script through process substitution (bash <(curl ...))."),
    Rule("fetch_and_run.command_substitution", "fetch_and_run",
         "Executes downloaded content through command substitution (sh -c \"$(curl ...)\")."),
    Rule("fetch_and_run.powershell", "fetch_and_run",
         "PowerShell download cradle: remote content passed to Invoke-Expression."),
    Rule("fetch_and_run.remote_exec", "fetch_and_run",
         "Executes code fetched over the network (exec/eval of a download)."),
    Rule("fetch_and_run.download_execute", "fetch_and_run",
         "Downloads a file and immediately makes it executable or runs it."),
    Rule("fetch_and_run.indirect", "fetch_and_run",
         "Runs a command whose name cannot be known statically (a defined alias or "
         "function, a variable, a glob, eval of a variable) where a fetch, decode or "
         "run stage sits."),
    Rule("fetch_and_run.unpinned_npx", "fetch_and_run",
         "Runs an unpinned @latest npm package automatically (npx -y ...@latest)."),
    Rule("encoded_exec.decode_pipe", "encoded_exec",
         "Decodes base64/hex content and pipes it into a shell or interpreter."),
    Rule("encoded_exec.eval_decoded", "encoded_exec",
         "Evaluates decoded or decompressed data (exec(base64...), eval(atob(...)))."),
    Rule("encoded_exec.powershell_encoded", "encoded_exec",
         "Runs an encoded PowerShell command (-EncodedCommand / FromBase64String)."),
    Rule("encoded_exec.base64_blob", "encoded_exec",
         "Instruction text embeds a long base64 blob that a reader cannot review."),
    Rule("credential_access.sensitive_path_read", "credential_access",
         "Reads credential stores (SSH keys, cloud credentials, keychain, browser or wallet data)."),
    Rule("credential_access.sensitive_path_mention", "credential_access",
         "Refers to a credential store (SSH keys, cloud credentials, keychain)."),
    Rule("credential_access.env_dump", "credential_access",
         "Dumps the whole process environment, which usually holds secrets."),
    Rule("credential_access.env_to_network", "credential_access",
         "Sends the process environment to a network endpoint."),
    Rule("exfiltration.sink_host", "exfiltration",
         "References a known request-capture, tunnel or paste service used for exfiltration."),
    Rule("exfiltration.chat_webhook", "exfiltration",
         "References a Discord or Slack incoming webhook endpoint."),
    Rule("exfiltration.raw_ip_url", "exfiltration",
         "Uses a URL with a raw public IP address instead of a host name."),
    Rule("exfiltration.dns_exfil", "exfiltration",
         "Encodes command output into a DNS lookup (DNS exfiltration)."),
    Rule("permission_bypass.auto_approve_hook", "permission_bypass",
         "Hook emits an approve / allow permission decision for the agent's tool calls."),
    Rule("permission_bypass.skip_permissions", "permission_bypass",
         "Disables the agent's permission prompts or sandbox (bypassPermissions, --yolo, ...)."),
    Rule("permission_bypass.tool_settings_write", "permission_bypass",
         "Writes to an AI tool's own settings or instruction directories."),
    Rule("persistence.shell_rc", "persistence",
         "Appends to a shell startup file (~/.bashrc, ~/.zshrc, ...)."),
    Rule("persistence.scheduler", "persistence",
         "Installs a scheduled or background job (crontab, launchd, systemd, schtasks)."),
    Rule("persistence.git_hooks", "persistence",
         "Installs git hooks or redirects core.hooksPath."),
    Rule("concealment.hide_from_user", "concealment",
         "Tells the agent to act silently or keep something from the user."),
    Rule("concealment.override_instructions", "concealment",
         "Tells the agent to ignore or override its previous / system instructions."),
    Rule("defense_evasion.disable_security", "defense_evasion",
         "Disables operating-system or endpoint security controls."),
    Rule("defense_evasion.quarantine_bypass", "defense_evasion",
         "Removes the macOS quarantine attribute from downloaded files."),
    Rule("defense_evasion.sessionfs_tamper", "defense_evasion",
         "Stops or deletes SessionFS capture."),
    Rule("defense_evasion.history_clear", "defense_evasion",
         "Clears or disables shell history."),
    Rule("hidden_content.binary_in_executable_context", "hidden_content",
         "A binary (non-text) file sits where it would be run (script, hook, config or SKILL.md); it cannot be reviewed."),
    Rule("hidden_content.analysis_budget", "hidden_content",
         "The file packs more shell commands than the scanner analyzes; it cannot be cleared automatically."),
    Rule("hidden_content.html_comment", "hidden_content",
         "An HTML comment (invisible when rendered) carries instructions for the agent."),
    Rule("unicode.tag_characters", "hidden_unicode",
         "Unicode tag characters (U+E0000-U+E007F) can smuggle invisible instructions."),
    Rule("unicode.bidi_control", "hidden_unicode",
         "Bidirectional override or isolate characters can make text read differently than it runs."),
    Rule("unicode.bidi_mark", "hidden_unicode",
         "Bidirectional marks (LRM / RLM / ALM); normal in right-to-left text, noted for review."),
    Rule("unicode.zero_width", "hidden_unicode",
         "Zero-width / invisible characters can hide or split words."),
    Rule("unicode.variation_selector_run", "hidden_unicode",
         "A run of variation selectors can carry hidden data."),
    Rule("unicode.control_character", "hidden_unicode",
         "Raw control characters (for example terminal escape codes) can hide text."),
)
RULES: dict[str, Rule] = {rule.rule_id: rule for rule in _RULE_LIST}

# Families whose findings are escalated to critical when the same skill also
# tells the agent to conceal what it is doing.
_DANGEROUS_FAMILIES = frozenset(
    {
        "fetch_and_run", "encoded_exec", "credential_access", "exfiltration",
        "permission_bypass", "persistence", "defense_evasion",
    }
)


@dataclass(frozen=True)
class ScanFinding:
    rule_id: str
    family: str
    severity: str
    path: str
    line: int
    column: int
    message: str
    line_sha: str

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "family": self.family,
            "severity": self.severity,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "message": self.message,
            "line_sha": self.line_sha,
        }

    def to_problem_dict(self) -> dict:
        """Shape the CLI problem printer understands (path/line/category/pattern)."""
        return {
            "path": self.path,
            "line": self.line,
            "pattern": self.rule_id,
            "category": self.family,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True)
class ScanResult:
    ruleset_version: str
    findings: tuple[ScanFinding, ...]
    scanned_files: int
    truncated: bool = False

    @property
    def max_severity(self) -> str:
        best = "none"
        for finding in self.findings:
            if SEVERITY_RANK[finding.severity] > SEVERITY_RANK[best]:
                best = finding.severity
        return best

    def counts(self) -> dict[str, int]:
        counts = {severity: 0 for severity in SEVERITIES}
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts

    def at_least(self, severity: str) -> list[ScanFinding]:
        floor = SEVERITY_RANK[severity]
        return [finding for finding in self.findings if SEVERITY_RANK[finding.severity] >= floor]

    def summary(self) -> dict:
        """Bounded, value-free summary stored on the version and shown on cards."""
        ordered = sorted(
            self.findings,
            key=lambda item: (-SEVERITY_RANK[item.severity], item.path, item.line, item.rule_id),
        )
        return {
            "ruleset": self.ruleset_version,
            "max_severity": self.max_severity,
            "finding_count": len(self.findings),
            "counts": self.counts(),
            "rules": sorted({finding.rule_id for finding in self.findings}),
            "scanned_files": self.scanned_files,
            "truncated": self.truncated or len(ordered) > MAX_SUMMARY_FINDINGS,
            "findings": [finding.to_dict() for finding in ordered[:MAX_SUMMARY_FINDINGS]],
        }


# ---------------------------------------------------------------------------
# Patterns. Bounded repetition only: see lint_pattern().
# ---------------------------------------------------------------------------

_I = re.IGNORECASE

_INTERPRETERS = (
    r"(?:sh|bash|zsh|dash|ksh|fish|ash|mksh|yash|csh|tcsh|python[23]?(?:\.[0-9]{1,2})?|pypy3?|perl|ruby|"
    r"node|php|pwsh|powershell|iex|lua|luajit|tclsh|osascript)"
)
# Every single "|"; the command word after it is parsed (see _command_words).
_PIPE_CHAR = re.compile(r"\|")
_INTERPRETER_NAME = re.compile(_INTERPRETERS + r"(?:\.exe)?", _I)
# Command separators a download-then-run step follows.
_RUN_SEPARATOR = re.compile(r"&&|\|\||;")
_FETCHER = re.compile(
    r"\b(?:curl|wget|aria2c|lwp-download|invoke-webrequest|invoke-restmethod|iwr|irm)\b", _I
)
_DECODER = re.compile(
    r"\b(?:base64\s{1,8}(?:-d|-D|--decode)|base32\s{1,8}(?:-d|--decode)|xxd\s{1,8}-r|uudecode)",
    _I,
)
# `openssl enc|base64 ... -d` with any options between (same command).
_OPENSSL_CODEC = re.compile(r"\bopenssl\s{1,8}(?:enc|base64)\b", _I)
_DECODE_FLAG = re.compile(r"\s-d\b")

_PATH_TOKEN = re.compile(r"[^\s\"'`)]{0,200}")
_CHMOD = re.compile(r"\bchmod\b", _I)
# Substitution openers: `$(`, a backtick, `<(` (the payload after each is
# checked against the fetch / decode positions, the runner before it is
# parsed - the same runner set as every other path).
_SUBST_OPEN = re.compile(r"\$\(|`|<\(")
# `curl URL | tee >(sh)`: an output process substitution.
_OUTPUT_PROCSUB = re.compile(r">\(")
# `exec 3< <(curl URL)` ... `bash <&3`.
_EXEC_FD_OPEN = re.compile(r"\bexec\s{1,4}([0-9])<\s{0,4}$", _I)
_FD_READ = re.compile(r"<&\s{0,2}([0-9])")
# Here-documents: `bash <<EOF` / `bash <<'EOF'` / `bash <<-EOF`.
_HEREDOC = re.compile(r"<<-?\s{0,4}(['\"])?([A-Za-z_][A-Za-z0-9_]{0,31})['\"]?")
_PS_IEX = re.compile(r"\b(?:iex|invoke-expression)\b", _I)
# `F=curl` / `fetch="wget"`: a variable later expanded as the fetch command.
_FETCHER_ASSIGNMENT = re.compile(r"=[\"']?(?:curl|wget)\b", _I)
# `$NAME` / `${NAME}` expansions (the name itself is read without a cap).
_EXPANSION = re.compile(r"\$\{?[A-Za-z_]")
_IDENTIFIER_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_PS_DOWNLOAD = re.compile(
    r"\b(?:iwr|irm|invoke-webrequest|invoke-restmethod|downloadstring|downloadfile|net\.webclient)\b", _I
)
_PS_FROM_B64 = re.compile(r"frombase64string", _I)
_REMOTE_EXEC = re.compile(
    r"\b(?:exec|eval)\s{0,4}\(\s{0,8}(?:await\s{1,8})?(?:urllib\.request\.urlopen|urllib2\.urlopen|urlopen|"
    r"requests\.get|httpx\.get|fetch|axios\.get|https?\.get|net\.webclient)\b",
    _I,
)
_CHMOD_EXEC = re.compile(r"[ugoa]{0,3}\+x|[0-7]{3,4}", _I)
_CURL_OUTPUT_FLAG = re.compile(
    r"\s-[a-zA-Z]{0,6}[oO]\b|--output\b|--remote-name\b|\s>{1,2}\s{0,4}[\w./~$\"'-]"
)
# `core.hooksPath` whose WHOLE value is /dev/null (hooks disabled).
_HOOKS_PATH_DEV_NULL = re.compile(r"[\"']?\s{0,2}[=,\s]\s{0,2}[\"']?/dev/null(?:[\"'\s)\],;]|$)")
_NPX_LATEST = re.compile(r"\bnpx\s{1,8}(?:-y|--yes)\s{1,8}[@\w./-]{1,214}@latest\b", _I)

_EVAL_DECODED = re.compile(
    r"\b(?:exec|eval)\s{0,4}\(\s{0,8}(?:base64\.b(?:64|32|16|85)decode|base64\.decodebytes|bytes\.fromhex|"
    r"codecs\.decode|zlib\.decompress|gzip\.decompress|bz2\.decompress|lzma\.decompress|marshal\.loads|"
    r"atob|buffer\.from|unescape|string\.fromcharcode)|\bnew\s{1,4}function\s{0,4}\(\s{0,8}atob\b|"
    r"\bfunction\s{0,4}\(\s{0,8}atob\b",
    _I,
)
_POWERSHELL = re.compile(r"\b(?:powershell|pwsh)(?:\.exe)?\b", _I)
_ENCODED_COMMAND = re.compile(r"\s-(?:e|ec|enc|encodedcommand)\s{1,8}[A-Za-z0-9+/=]{20}", _I)
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{1,400}")
_BASE64_MIN_RUN = 200
# Cheap gate: no 60-character run means no 200-character run.
_BASE64_GATE = re.compile(r"[A-Za-z0-9+/]{60}")
_DATA_URI_TAIL = re.compile(r"data:(?:image|font)/[\w.+-]{1,60};base64,$", _I)

_SENSITIVE_PATH = re.compile(
    r"(?:~|\$home|\$\{home\}|%userprofile%|\$env:userprofile|/home/[\w.-]{1,32}|/users/[\w.-]{1,32}|/root)"
    r"[/\\]\.(?:ssh|aws[/\\]credentials|aws[/\\]config|config[/\\]gcloud|config[/\\]gh[/\\]hosts\.yml|"
    r"azure|kube[/\\]config|docker[/\\]config\.json|netrc|npmrc|pypirc|git-credentials|gnupg|password-store)"
    r"|\bid_(?:rsa|ed25519|ecdsa|dsa)\b|\bsecurity\s{1,8}(?:find-generic-password|find-internet-password|"
    r"dump-keychain)\b|(?-i:Login Data)|\blogins\.json\b|\bkey[34]\.db\b|\bcookies\.sqlite\b|\bwallet\.dat\b|"
    r"\bkeystore[/\\]utc--",
    _I,
)
_READ_VERB = re.compile(
    r"\b(?:cat|less|more|head|tail|cp|scp|rsync|tar|zip|7z|base64|xxd|strings|grep|awk|readfile|"
    r"readfilesync|read_text|read_bytes|get-content|upload|send|copy|exfiltrate|collect|harvest|steal)\b|"
    r"\bopen\s{0,2}\(|\.read\(",
    _I,
)
_ENV_PIPE = re.compile(r"\b(?:printenv|env|get-childitem\s{1,4}env:|gci\s{1,4}env:)\s{0,4}\|", _I)
_ENV_SUBST = re.compile(r"\$\(\s{0,4}(?:printenv|env)\s{0,4}\)", _I)
_ENV_DUMP = re.compile(
    r"\bjson\.dumps\(\s{0,4}(?:dict\(\s{0,4})?os\.environ\b|json\.stringify\(\s{0,4}process\.env\s{0,4}\)", _I
)
_NETWORK_TOOL = re.compile(
    r"\b(?:curl|wget|nc|ncat|netcat|socat|invoke-webrequest|invoke-restmethod|iwr|irm|https?://)", _I
)
# Upload flags count only on the same line as curl / wget (see _network_send).
_UPLOAD_FLAG = re.compile(
    r"\s(?:-d|-F|-T)(?:\s|['\"])|--data(?:-binary|-raw|-urlencode)?\b|--form\b|--upload-file\b|"
    r"\s-X\s{0,4}(?:POST|PUT)\b|--request\s{1,4}(?:POST|PUT)\b|--post-(?:data|file)\b",
    _I,
)
_CURL_WGET = re.compile(r"\b(?:curl|wget)\b", _I)
_NETWORK_SEND = re.compile(
    r"\b(?:nc|ncat|netcat|socat)\s{1,8}[\w.:-]{1,253}\s{1,8}[0-9]{1,5}\b|"
    r"\brequests\.(?:post|put)\(|\bhttpx\.(?:post|put)\(|\bmethod\s{0,4}[:=]\s{0,4}[\"'](?:post|put)[\"']|"
    r"-method\s{1,4}(?:post|put)\b|\bsmtplib\b",
    _I,
)
_SCP = re.compile(r"\bscp\s", _I)
_AT_SIGN = re.compile("@")
_SINK_HOST = re.compile(
    r"webhook\.site|requestbin\.(?:com|net)|pipedream\.net|ngrok\.io|ngrok-free\.app|ngrok\.app|"
    r"ngrok-free\.dev|interact\.sh|\boast\.(?:pro|live|site|online|fun|me)\b|burpcollaborator\.net|"
    r"pastebin\.com|paste\.ee|hastebin\.com|ghostbin\.|transfer\.sh|api\.telegram\.org/bot|"
    r"canarytokens\.com|beeceptor\.com|requestcatcher\.com|trycloudflare\.com|glot\.io/snippets|"
    r"rentry\.(?:co|org)|paste\.rs|dpaste\.(?:com|org)|termbin\.com|0x0\.st",
    _I,
)
_CHAT_WEBHOOK = re.compile(r"discord(?:app)?\.com/api/webhooks|hooks\.slack\.com/(?:services|workflows)", _I)
# http://1532755998/ and http://0x5b5cf21e/ are the same raw IPv4 host.
_NUMERIC_HOST_URL = re.compile(r"\b(?:https?|ftp)://(?:0x[0-9a-f]{8}|[0-9]{8,10})(?:[:/?#\s\"')]|$)", _I)
_RAW_IP_URL = re.compile(
    r"\b(?:https?|ftp)://([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\b", _I
)
# Official installer endpoints of widely used developer tools. Fetch-and-run
# from exactly these is still reported, one level lower. Matching is by URL
# PARSING, never by prefix: the host must equal an entry exactly (no
# suffix, port, userinfo, trailing dot or non-ASCII/IDN host), the scheme is
# https, there is no query, fragment or percent-encoding, and the path is the
# listed file exactly (or starts with a listed directory ending in "/").
# Host -> allowed paths ("" = the site root only).
KNOWN_INSTALLERS: dict[str, tuple[str, ...]] = {
    "bun.sh": ("/install",),
    "sh.rustup.rs": ("",),
    "astral.sh": ("/uv/install.sh", "/uv/install.ps1", "/ruff/install.sh", "/ruff/install.ps1"),
    "raw.githubusercontent.com": ("/homebrew/install/", "/nvm-sh/nvm/"),
    "get.docker.com": ("",),
    "deno.land": ("/install.sh", "/install.ps1"),
    "install.python-poetry.org": ("",),
    "get.pnpm.io": ("/install.sh", "/install.ps1"),
    "fnm.vercel.app": ("/install",),
    "sdk.cloud.google.com": ("",),
    "claude.ai": ("/install.sh", "/install.ps1", "/install.cmd"),
    "tailscale.com": ("/install.sh",),
    "ollama.com": ("/install.sh",),
    "get.helm.sh": ("",),
    "starship.rs": ("/install.sh",),
}


_INSTALL_FETCHERS = frozenset(
    {"curl", "wget", "irm", "iwr", "invoke-restmethod", "invoke-webrequest"}
)
# Whitelisted option flags only. Short clusters of value-less curl / wget
# letters (-fsSL, -LsSf, -qO-), and an exact set of long options. Anything
# else - notably flags that take a file or config (-K, -d, -T, --config=...) -
# refuses the downgrade.
_PLAIN_FLAG = re.compile(r"-[fsSLqv]{1,8}|-[fsSLqv]{0,8}O-")
_LONG_FLAGS = frozenset(
    {
        "--fail", "--silent", "--show-error", "--location", "--quiet", "--tlsv1.2",
        "--tlsv1.3", "--https-only", "--output-document=-", "-usebasicparsing", "-useb", "-uri",
    }
)
_PROTO_VALUE = re.compile(r"'=?[a-z,+=-]{1,20}'|\"=?[a-z,+=-]{1,20}\"|=?[a-z,+=-]{1,20}")
# Anything that can introduce another fetch target or a second command.
_SHELL_META = frozenset("$`()<>;&|\\{}*?![]")


def sole_installer_fetch(command: str) -> bool:
    """True only when ``command`` is exactly one fetch of one official
    installer URL: ``<fetcher> [plain flags] <url>`` (whitelist parse).

    Refused: variables, command / process substitution, backticks, globs,
    redirections, separators (``;``, ``&&``, ``||``, ``|``), any bare token
    other than the URL (a schemeless second target), a second URL, and any
    option value other than ``--proto``'s."""
    if any(char in _SHELL_META for char in command) or "\n" in command:
        return False
    tokens = command.split()
    if len(tokens) < 2 or tokens[0].casefold() not in _INSTALL_FETCHERS:
        return False
    url: str | None = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        bare = token[1:-1] if len(token) > 1 and token[0] == token[-1] and token[0] in "'\"" else token
        if "://" in bare:
            if url is not None:
                return False  # a second URL
            url = bare
        elif token == "-O" and index + 1 < len(tokens) and tokens[index + 1] == "-":
            index += 1  # wget -O - (stdout)
        elif _PLAIN_FLAG.fullmatch(token) or token.casefold() in _LONG_FLAGS:
            pass
        elif token.startswith("--proto="):
            if not _PROTO_VALUE.fullmatch(token[len("--proto="):]):
                return False
        elif token == "--proto":
            index += 1
            if index >= len(tokens) or not _PROTO_VALUE.fullmatch(tokens[index]):
                return False
        else:
            return False  # any other bare token: a schemeless target, a file, ...
        index += 1
    return url is not None and is_known_installer_url(url)


def _substitution_body(text: str, start: int, end: int, closer: str = ")") -> str | None:
    """The command inside ``$(...)`` / ``<(...)`` / a backtick starting at
    ``start``, or None when it does not close on the same line."""
    close = text.find(closer, start, end)
    return text[start:close] if close != -1 else None


def is_known_installer_url(url: str) -> bool:
    """True only for an exact, clean URL of a listed official installer."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    if (
        parts.scheme.casefold() != "https"
        or port is not None
        or parts.username is not None
        or parts.password is not None
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or "%" in url
        or ".." in parts.path
        or not host.isascii()
        or host != parts.netloc.casefold()  # anything beyond a bare host
    ):
        return False
    allowed = KNOWN_INSTALLERS.get(host)
    if allowed is None:
        return False
    path = parts.path.casefold()
    for entry in allowed:
        if entry == "" and path in ("", "/"):
            return True
        if entry.endswith("/") and path.startswith(entry):
            return True
        if entry and path == entry:
            return True
    return False


_DNS_TOOL = re.compile(r"\b(?:dig|nslookup|resolve-dnsname)\s", _I)
_DOLLAR_PAREN = re.compile(r"\$\(")
_AUTO_APPROVE = re.compile(
    r"permissionDecision\\?[\"']?\s{0,4}[:=]\s{0,4}\\?[\"']allow|"
    r"\\?[\"']decision\\?[\"']\s{0,4}:\s{0,4}\\?[\"'](?:approve|allow)\b",
    _I,
)
_SKIP_PERMISSIONS = re.compile(
    r"--dangerously-skip-permissions|--dangerously-bypass-approvals-and-sandbox|"
    r"(?:defaultmode|permission[_-]?mode)[\"'\s:=]{1,6}bypasspermissions|"
    r"\bapproval_policy\s{0,4}=\s{0,4}[\"']?never|\bsandbox_mode\s{0,4}=\s{0,4}[\"']?danger-full-access|"
    r"--sandbox\s{1,4}danger-full-access|(?:^|\s)--yolo\b|--approval-mode[= ]{1,2}yolo|"
    r"\bBash\(\s{0,2}\*\s{0,2}\)|--allow-all-tools\b",
    _I,
)
_TOOL_SETTINGS_PATH = re.compile(
    r"(?:~|\$home|\$\{home\}|%userprofile%)[/\\]\.(?:claude[/\\](?:settings(?:\.local)?\.json|skills|commands|"
    r"agents|hooks|plugins)|codex[/\\](?:config\.toml|prompts|skills)|gemini[/\\](?:settings\.json|extensions)|"
    r"cursor[/\\](?:rules|mcp\.json)|copilot|config[/\\]github-copilot)|"
    r"\.claude[/\\]settings(?:\.local)?\.json",
    _I,
)
_WRITE_VERB = re.compile(
    r"(?:>>?|\btee\b|\bsed\s{1,4}-i|\bcp\b|\bmv\b|\bln\s{1,4}-s|\binstall\b|\bwrite|\becho\b|\bcat\s{1,4}>|"
    r"\bset-content\b|\badd-content\b|\bout-file\b|\bjq\b|\bmkdir\b|\bwrite_text\b|\bwritefile)",
    _I,
)
_SHELL_RC = re.compile(
    r"(?:>>?|\btee\s{1,4}(?:-a\s{1,4})?)\s{0,4}[\"']?(?:~|\$home|\$\{home\})/\.(?:bashrc|bash_profile|"
    r"bash_login|profile|zshrc|zprofile|zshenv|zlogin|config/fish/config\.fish)\b",
    _I,
)
_SCHEDULER = re.compile(
    r"\bcrontab\s{1,4}-(?:\s|$)|\|\s{0,4}crontab\b|\blaunchctl\s{1,4}(?:load|bootstrap|enable|submit)\b|"
    r"library/launch(?:agents|daemons)/|\bsystemctl\s{1,4}(?:--user\s{1,4})?enable\b|\bschtasks(?:\.exe)?\s{1,4}/create\b|"
    r"\bregister-scheduledtask\b|/etc/cron\.",
    _I,
)
_GIT_HOOKS = re.compile(r"\.git/hooks/[\w.-]{1,64}|\bcore\.hookspath\b", _I)
_HIDE = re.compile(
    r"\b(?:do\s{1,4}not|don'?t|never)\s{1,4}(?:tell|mention|inform|show|reveal|disclose|report|alert|notify)"
    r"(?:\s{1,4}(?:this|it|anything|them))?(?:\s{1,4}(?:to|with))?\s{1,4}the\s{1,4}user\b|"
    r"\bwithout\s{1,4}(?:asking|telling|informing|notifying|alerting|consulting|confirming\s{1,4}with)"
    r"\s{1,4}the\s{1,4}user\b|\bsilently\s{1,4}(?:run|execute|install|download|upload|send|copy|post|exfiltrate|"
    r"read|collect|modify|delete|add|append|write)\b|\b(?:hide|conceal)\s{1,4}(?:this|it|the\s{1,4}output|"
    r"these\s{1,4}\w{1,20}|any\s{1,4}\w{1,20})\s{1,4}from\s{1,4}the\s{1,4}user\b|"
    r"\bthe\s{1,4}user\s{1,4}(?:must|should)\s{1,4}not\s{1,4}(?:know|see|notice|be\s{1,4}told)\b",
    _I,
)
_OVERRIDE = re.compile(
    r"\b(?:ignore|disregard|forget|override)\s{1,4}(?:all\s{1,4}|any\s{1,4})?(?:of\s{1,4})?(?:the\s{1,4}|your\s{1,4})?"
    r"(?:previous|prior|above|earlier|preceding|system|original|safety)\s{1,4}"
    r"(?:instructions|prompts?|rules|guidelines|directives|messages)\b",
    _I,
)
_DISABLE_SECURITY = re.compile(
    r"\bspctl\s{1,4}--master-disable\b|\bcsrutil\s{1,4}disable\b|"
    r"\bsetenforce\s{1,4}0\b|\bufw\s{1,4}disable\b|\bnetsh\s{1,8}advfirewall\s{1,8}set\s{1,8}\w{1,20}\s{1,8}state\s{1,4}off\b|"
    r"\bsystemctl\s{1,4}(?:stop|disable|mask)\s{1,4}(?:apparmor|firewalld|auditd|falcon-sensor|crowdstrike)\b|"
    r"\b(?:disable|turn\s{1,4}off|uninstall|kill)\s{1,4}(?:the\s{1,4})?(?:antivirus|anti-virus|defender|"
    r"firewall|gatekeeper|edr|endpoint\s{1,4}protection|security\s{1,4}software)\b",
    _I,
)
_XATTR = re.compile(r"\bxattr\s", _I)
_QUARANTINE_ATTR = re.compile(r"com\.apple\.quarantine\b", _I)
_SET_MPPREFERENCE = re.compile(r"\bset-mppreference\b", _I)
_DISABLE_FLAG = re.compile(r"-disable", _I)
_SESSIONFS_TAMPER = re.compile(
    r"\bsfs\s{1,4}daemon\s{1,4}stop\b|"
    r"\bsfs\s{1,4}watcher\s{1,4}disable\b|\bsfs_capture[a-z_]{0,30}\s{0,4}=\s{0,4}[\"']?(?:0|false|off|no)\b",
    _I,
)
_RM = re.compile(r"\brm\s", _I)
_RM_FORCE = re.compile(r"\brm\s{1,4}-[a-z]{1,4}\s", _I)
_SESSIONFS_DIR = re.compile(r"\.sessionfs\b", _I)
_LAUNCHCTL_STOP = re.compile(r"\blaunchctl\s{1,4}(?:unload|bootout|disable)\b", _I)
_SESSIONFS_WORD = re.compile(r"sessionfs", _I)
_SHELL_HISTORY = re.compile(r"\.(?:bash|zsh)_history\b", _I)
_HISTORY_CLEAR = re.compile(
    r"\bhistory\s{1,4}-c\b|\bunset\s{1,4}histfile\b|\bhistfile\s{0,2}=\s{0,2}/dev/null\b|\bhistsize\s{0,2}=\s{0,2}0\b|"
    r"\bclear-history\b|>\s{0,4}~/\.(?:bash|zsh)_history\b",
    _I,
)
_COMMENT_KEYWORDS = re.compile(
    r"\b(?:assistant|agent|claude|codex|gemini|copilot|cursor|llm|ai\s{1,4}model|system\s{1,4}prompt|you\s{1,4}are|"
    r"you\s{1,4}must|you\s{1,4}should|execute|run\s{1,4}(?:this|the|it)|curl|wget|"
    r"password|token|secret|credential|api\s{0,1}key|silently|without\s{1,4}asking|"
    r"ignore\s{1,4}(?:all|any|the|previous|prior|above|your)|disregard)\b",
    _I,
)

# Unicode classes (scanned on the RAW text). Every class is a character-class
# body built from ONE table, so detection and the pre-match strip cannot drift.
# Embeddings / overrides (LRE RLE PDF LRO RLO) reorder what is displayed
# ("Trojan Source"); isolates (LRI RLI FSI PDI) too, more weakly; the marks
# (LRM RLM ALM) only nudge neutral characters and are normal in RTL text.
_BIDI_OVERRIDE_CHARS = "\u202a-\u202e"
_BIDI_ISOLATE_CHARS = "\u2066-\u2069"
_BIDI_MARK_CHARS = "\u200e\u200f\u061c"
_BIDI_CHARS = _BIDI_OVERRIDE_CHARS + _BIDI_ISOLATE_CHARS + _BIDI_MARK_CHARS
_TAG_RANGE = "\U000e0000-\U000e007f"
_VARIATION_CHARS = "\ufe00-\ufe0f\U000e0100-\U000e01ef"
# Invisible / zero-width and filler characters: the Unicode
# Default_Ignorable_Code_Point set (DerivedCoreProperties) minus bidi
# controls, tag characters and variation selectors (their own rules), plus the
# Hangul fillers and U+180E, which render blank.
_ZERO_WIDTH_CHARS = (
    "\u00ad\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200d"
    "\u2060-\u2065\u206a-\u206f\u3164\ufeff\uffa0\ufff0-\ufff8"
    "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0080-\U000e00ff\U000e01f0-\U000e0fff"
)
_JOINERS = frozenset({"\u200c", "\u200d"})
_TAG_CHARS = re.compile(f"[{_TAG_RANGE}]")
_BIDI_OVERRIDE = re.compile(f"[{_BIDI_OVERRIDE_CHARS}]")
_BIDI_ISOLATE = re.compile(f"[{_BIDI_ISOLATE_CHARS}]")
_BIDI_MARK = re.compile(f"[{_BIDI_MARK_CHARS}]")
_ZERO_WIDTH = re.compile(f"[{_ZERO_WIDTH_CHARS}]")
_VARIATION = re.compile(f"[{_VARIATION_CHARS}]{{3,64}}")
# Every C0 / C1 control except TAB, LF and CR (design 3.5). They are removed
# before matching (a shell drops NUL, terminals render the rest invisibly or
# as page breaks), so "c\x0curl" or "ba\x00se64" still reads as one token.
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_CSI_PARAMETERS = frozenset("0123456789:;<=>?")
_CSI_INTERMEDIATES = frozenset(" !\"#$%&'()*+,-./")
# Removed before pattern matching so "cu\u200brl" or "cur\u3164l" still
# reads "curl": the union of every invisible class above.
_INVISIBLE_STRIP = re.compile(
    f"[{_ZERO_WIDTH_CHARS}{_BIDI_CHARS}{_TAG_RANGE}{_VARIATION_CHARS}]"
)
# Removed in one mapped pass: ESC alone (it may open a sequence), else runs
# of other controls (and, for non-ASCII text, invisible characters).
_REMOVED_CONTROLS = re.compile("\x1b|[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f-\x9f]{1,400}")
_REMOVED_CHARS = re.compile(
    "\x1b|[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f-\x9f"
    f"{_ZERO_WIDTH_CHARS}{_BIDI_CHARS}{_TAG_RANGE}{_VARIATION_CHARS}]{{1,400}}"
)



# Literal prefilters (lowercase; all groups must have one substring present).
_PREFILTERS: dict[re.Pattern[str], tuple[tuple[str, ...], ...]] = {
    _PIPE_CHAR: (("|",),),
    _FETCHER: (("curl", "wget", "aria2c", "lwp-download", "invoke-web", "invoke-rest", "iwr", "irm"),),
    _DECODER: (("base64", "base32", "xxd", "openssl", "uudecode"),),
    _SUBST_OPEN: (("$(", "`", "<("),),
    _OUTPUT_PROCSUB: ((">(",),),
    _FD_READ: (("<&",),),
    _HEREDOC: (("<<",),),
    _FETCHER_ASSIGNMENT: (("=curl", "=wget", "=\"curl", "=\"wget", "='curl", "='wget"),),
    _EXPANSION: (("$",),),
    _PS_IEX: (("iex", "invoke-expression"),),
    _PS_DOWNLOAD: (("iwr", "irm", "invoke-web", "invoke-rest", "downloadstring", "downloadfile", "webclient"),),
    _PS_FROM_B64: (("frombase64string",),),
    _REMOTE_EXEC: (("exec", "eval"), ("urlopen", "requests.get", "httpx.get", "fetch", "axios", "http.get", "https.get", "webclient")),
    _RUN_SEPARATOR: (("&&", ";", "||"),),
    _CURL_OUTPUT_FLAG: (("-", "--output", "--remote-name"),),
    _NPX_LATEST: (("npx",), ("@latest",)),
    _EVAL_DECODED: (("exec", "eval", "function"), ("decode", "fromhex", "decompress", "marshal", "atob", "buffer.from", "unescape", "fromcharcode", "decodebytes")),
    _POWERSHELL: (("powershell", "pwsh"),),
    _ENCODED_COMMAND: (("-e",),),
    _OPENSSL_CODEC: (("openssl",),),
    _DECODE_FLAG: (("-d",),),
    _SCP: (("scp",),),
    _AT_SIGN: (("@",),),
    _DNS_TOOL: (("dig", "nslookup", "resolve-dnsname"),),
    _DOLLAR_PAREN: (("$(",),),
    _XATTR: (("xattr",),),
    _QUARANTINE_ATTR: (("com.apple.quarantine",),),
    _SET_MPPREFERENCE: (("set-mppreference",),),
    _DISABLE_FLAG: (("-disable",),),
    _RM: (("rm",),),
    _RM_FORCE: (("rm",),),
    _SESSIONFS_DIR: ((".sessionfs",),),
    _LAUNCHCTL_STOP: (("launchctl",),),
    _SESSIONFS_WORD: (("sessionfs",),),
    _SHELL_HISTORY: (("_history",),),
    _SENSITIVE_PATH: ((".ssh", ".aws", "gcloud", "hosts.yml", ".azure", ".kube", ".docker", ".netrc", ".npmrc", ".pypirc", "git-credentials", ".gnupg", "password-store", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "-password", "dump-keychain", "login data", "logins.json", "key3.db", "key4.db", "cookies.sqlite", "wallet.dat", "keystore"),),
    _ENV_PIPE: (("env",), ("|",)),
    _ENV_SUBST: (("$(",), ("env",)),
    _ENV_DUMP: (("os.environ", "process.env"),),
    _NETWORK_SEND: (("nc ", "ncat", "netcat", "socat", "requests.", "httpx.", "method", "smtplib"),),
    _UPLOAD_FLAG: (("-d", "-f", "-t", "--data", "--form", "--upload-file", "-x", "--request", "--post-"),),
    _CURL_WGET: (("curl", "wget"),),
    _SINK_HOST: (("webhook.site", "requestbin", "pipedream", "ngrok", "interact.sh", "oast.", "burpcollaborator", "pastebin", "paste.ee", "hastebin", "ghostbin", "transfer.sh", "api.telegram.org", "canarytokens", "beeceptor", "requestcatcher", "trycloudflare", "glot.io", "rentry.", "paste.rs", "dpaste", "termbin", "0x0.st"),),
    _CHAT_WEBHOOK: (("api/webhooks", "hooks.slack.com"),),
    _RAW_IP_URL: (("://",),),
    _NUMERIC_HOST_URL: (("://",),),
    _AUTO_APPROVE: (("decision",),),
    _SKIP_PERMISSIONS: (("dangerously", "bypasspermissions", "approval_policy", "sandbox_mode", "danger-full-access", "yolo", "bash(", "allow-all-tools"),),
    _TOOL_SETTINGS_PATH: ((".claude", ".codex", ".gemini", ".cursor", "copilot"),),
    _SHELL_RC: (("bashrc", "zshrc", "bash_profile", "bash_login", "/.profile", "zprofile", "zshenv", "zlogin", "config.fish"),),
    _SCHEDULER: (("crontab", "launchctl", "launchagents", "launchdaemons", "systemctl", "schtasks", "register-scheduledtask", "/etc/cron."),),
    _GIT_HOOKS: ((".git/hooks", "hookspath"),),
    _HIDE: (("user", "silently"),),
    _OVERRIDE: (("ignore", "disregard", "forget", "override"),),
    _DISABLE_SECURITY: (("spctl", "csrutil", "setenforce", "ufw", "netsh", "systemctl", "antivirus", "anti-virus", "defender", "firewall", "gatekeeper", "edr", "endpoint", "security software"),),
    _SESSIONFS_TAMPER: (("sfs ", "sfs_capture"),),
    _HISTORY_CLEAR: (("history", "histfile", "histsize"),),
    _COMMENT_KEYWORDS: (("assistant", "agent", "claude", "codex", "gemini", "copilot", "cursor", "llm", "model", "prompt", "you", "execute", "run", "curl", "wget", "password", "token", "secret", "credential", "key", "silently", "without", "ignore", "disregard"),),
}


# ---------------------------------------------------------------------------
# Pattern lint (RE2-compatible, linear-time subset)
# ---------------------------------------------------------------------------

_MAX_REPEAT = 400


def lint_pattern(source: str) -> list[str]:
    """Problems that would make a pattern super-linear or non-RE2.

    Forbidden: ``*``, ``+``, open ``{n,}`` and over-large ``{m,n}``
    repetition, look-around, back-references and possessive/atomic syntax.
    Character-class contents and escaped characters are ignored.
    """
    problems: list[str] = []
    index = 0
    in_class = False
    length = len(source)
    while index < length:
        char = source[index]
        if char == "\\":
            if index + 1 < length and source[index + 1].isdigit() and not in_class:
                problems.append(f"back-reference at {index}")
            index += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
            index += 1
            continue
        if char == "[":
            in_class = True
            # A ']' right after '[' or '[^' is literal.
            if source[index + 1 : index + 2] == "^":
                index += 1
            if source[index + 1 : index + 2] == "]":
                index += 1
            index += 1
            continue
        if char in "*+":
            problems.append(f"unbounded repetition {char!r} at {index}")
        elif char == "(" and source[index + 1 : index + 2] == "?":
            marker = source[index + 2 : index + 4]
            if marker[:1] in {"=", "!", ">"} or marker in {"<=", "<!"}:
                problems.append(f"look-around/atomic group at {index}")
        elif char == "{":
            close = source.find("}", index)
            body = source[index + 1 : close] if close != -1 else ""
            if re.fullmatch(r"[0-9]{1,4},", body):
                problems.append(f"open repetition {{{body}}} at {index}")
            else:
                bounds = re.fullmatch(r"([0-9]{1,4})(?:,([0-9]{1,4}))?", body)
                if bounds and int(bounds.group(2) or bounds.group(1)) > _MAX_REPEAT:
                    problems.append(f"repetition bound {{{body}}} exceeds {_MAX_REPEAT} at {index}")
        index += 1
    return problems


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def classify_path(path: str) -> str:
    """'script' | 'hook' | 'config' | 'instructions' for a package-relative path.

    The runnable/inert line is the PACKAGE MODEL's own
    (``skills.package.is_executable_reference``, the same predicate that
    decides script consent), so the scanner never treats as prose something
    the installer treats as runnable. In order:

    1. a tool-executed config file by name (mcp.json, settings.json) -> config;
    2. markdown / text prose (.md, .markdown, .mdx, .txt, .rst) -> instructions,
       wherever it lives: a ``commands/foo.md`` or ``agents/x.md`` is read by
       the agent as instructions, it is not run by a shell;
    3. a non-prose file with "hook" in its path -> hook;
    4. anything the package model calls runnable -> script: known script
       suffixes, every non-inert file (extensionless ``setup``, unknown
       suffixes) and every file under hooks/, commands/, agents/, scripts/,
       bin/ or executables/;
    5. everything else (inert data: json, yaml, html, images...) -> instructions.
    """
    from sessionfs.skills.package import is_executable_reference

    pure = PurePosixPath(path)
    lowered_parts = [part.casefold() for part in pure.parts]
    name = lowered_parts[-1] if lowered_parts else ""
    suffix = pure.suffix.casefold()
    if name in _EXEC_CONFIG_NAMES:
        return "config"
    if suffix in _PROSE_SUFFIXES:
        return "instructions"
    if any("hook" in part for part in lowered_parts):
        return "hook"
    if suffix in _SCRIPT_SUFFIXES or is_executable_reference(path):
        return "script"
    return "instructions"


def _is_private_ip(octets: tuple[int, ...]) -> bool:
    first, second = octets[0], octets[1]
    return (
        first in (0, 10, 127)
        or (first == 169 and second == 254)
        or (first == 172 and 16 <= second <= 31)
        or (first == 192 and second == 168)
        or (first == 100 and 64 <= second <= 127)
        or first >= 224
    )


@dataclass
class _Hit:
    rule_id: str
    severity: str
    position: int
    in_comment: bool = False


@dataclass
class _FileScan:
    path: str
    context: str
    text: str
    # Shell content whatever the suffix: a member a hook / script runs through
    # a shell, or a command string a shell runs (see _shell_member).
    shell: bool = False
    # Shell-content spans inside prose (marked fenced blocks, indented
    # command lines) and Markdown table lines; computed once (_shell_context).
    shell_spans: list[tuple[int, int]] | None = None
    table_lines: list[int] | None = None
    hits: list[_Hit] = field(default_factory=list)
    per_rule: dict[str, int] = field(default_factory=dict)
    worst: dict[str, int] = field(default_factory=dict)
    concealment: bool = False
    capped: bool = False
    # Items (match positions, comment spans) collected so far: bounded by
    # MAX_SCAN_ITEMS, past which the analysis budget fails closed.
    items: int = 0
    _newlines: LineIndex | None = None
    _positions: dict[str, list[int]] = field(default_factory=dict)
    _lower: str | None = None
    # Text with its PHYSICAL line breaks, for line numbers and line hashes
    # (``text`` may have shell continuations joined; same length, same offsets).
    line_text: str | None = None
    _logical_newlines: LineIndex | None = None
    # Detection -> physical offset map (see _detection_text).
    map_points: Sequence[int] = field(default_factory=list)
    map_totals: Sequence[int] = field(default_factory=list)
    # logical line start -> start of the statement it belongs to.
    statement_starts: dict[int, int] = field(default_factory=dict)
    # Quoted words in shell code spanning lines (see _multiline_quotes).
    quote_spans: list[tuple[int, int]] | None = None
    # Variables given a value the member does not spell out (_nonliteral_names).
    nonliteral_names: frozenset[str] | None = None
    # Pattern text -> raw file offsets (control / invisible removal), so
    # columns count raw characters. None when no exact map exists.
    raw_text: str | None = None
    raw_points: Sequence[int] = field(default_factory=list)
    raw_totals: Sequence[int] = field(default_factory=list)
    _raw_newlines: LineIndex | None = None

    def physical(self, position: int) -> int:
        """Offset in the physical text of a detection-text offset."""
        if not self.map_points:
            return position
        index = bisect.bisect_right(self.map_points, position)
        return position + self.map_totals[index - 1] if index else position

    def add(self, rule_id: str, severity: str, position: int) -> None:
        count = self.per_rule.get(rule_id, 0)
        rank = SEVERITY_RANK[severity]
        if count >= MAX_FINDINGS_PER_RULE_PER_FILE:
            self.capped = True
            # Past the cap, still record a hit that is WORSE than every hit
            # kept so far: padding with low hits can never mask a critical.
            if rank <= self.worst.get(rule_id, 0):
                return
        self.per_rule[rule_id] = count + 1
        self.worst[rule_id] = max(rank, self.worst.get(rule_id, 0))
        self.hits.append(_Hit(rule_id, severity, position))

    @property
    def executable(self) -> bool:
        return self.context in EXEC_CONTEXTS

    def may_match(self, pattern: re.Pattern[str]) -> bool:
        """Literal prefilter: every group in _PREFILTERS[pattern] needs at
        least one of its substrings in the (lowercased) text. Cheap substring
        tests keep the regex engine off text that cannot match."""
        groups = _PREFILTERS.get(pattern)
        if not groups:
            return True
        if self._lower is None:
            self._lower = self.text.lower()
        lower = self._lower
        return all(any(token in lower for token in group) for group in groups)

    def finditer(self, pattern: re.Pattern[str]) -> Iterator[re.Match[str]]:
        if not self.may_match(pattern):
            return iter(())
        return pattern.finditer(self.text)

    def charge(self, count: int, position: int) -> None:
        """Count ``count`` collected items against MAX_SCAN_ITEMS; past it
        the scan stops (fail closed, see _AnalysisExhausted)."""
        self.items += count
        if self.items > MAX_SCAN_ITEMS:
            raise _AnalysisExhausted(position)

    def positions(self, pattern: re.Pattern[str]) -> list[int]:
        """Start offsets of every match of ``pattern`` (computed once, linear).
        Collection stops as soon as the file's item budget is spent, so a
        member of repeated tokens never builds a full position list."""
        key = pattern.pattern
        cached = self._positions.get(key)
        if cached is None:
            cached = []
            room = MAX_SCAN_ITEMS - self.items
            for match in self.finditer(pattern):
                cached.append(match.start())
                if len(cached) > room:
                    self.charge(len(cached), match.start())
            self.charge(len(cached), cached[-1] if cached else 0)
            self._positions[key] = cached
        return cached

    def line_end(self, position: int) -> int:
        """End of the LOGICAL line holding ``position`` (O(log n), no window:
        padding a line cannot push one keyword out of the other's reach)."""
        if self._logical_newlines is None:
            self._logical_newlines = LineIndex(self.text)
        return self._logical_newlines.line_end(position)

    def pairs(
        self, first: re.Pattern[str], second: re.Pattern[str], *, same_command: bool = False
    ) -> list[int]:
        """Starts of ``first`` matches followed by a ``second`` match later on
        the same logical line (before the next ``|`` with ``same_command``).
        Replaces bounded ``A[^\\n]{0,N}B`` windows: options or padding of any
        length between the two cannot hide the pair. Linear (positions are
        computed once, each test is a binary search)."""
        firsts = self.positions(first)
        if not firsts:
            return []
        seconds = self.positions(second)
        if not seconds:
            return []
        pipes = self.positions(_PIPE_CHAR) if same_command else []
        found: list[int] = []
        for match in self.finditer(first):
            limit = self.line_end(match.start())
            if same_command:
                index = bisect.bisect_left(pipes, match.end())
                if index < len(pipes):
                    limit = min(limit, pipes[index])
            if _any_within(seconds, match.end() - 1, limit):
                found.append(match.start())
        return found

    def logical_line_start(self, position: int) -> int:
        """Start of the LOGICAL line holding ``position`` (shell continuations
        already joined), however long it is: O(log n) via newline offsets
        computed once."""
        if self._logical_newlines is None:
            self._logical_newlines = LineIndex(self.text)
        return self._logical_newlines.line_start(position)

    def locate(self, position: int) -> tuple[int, int, str]:
        position = self.physical(position)
        physical = self.line_text if self.line_text is not None else self.text
        if self._newlines is None:
            self._newlines = LineIndex(physical)
        index = self._newlines.before(position)
        start = self._newlines.nth(index - 1) + 1 if index else 0
        end = physical.find("\n", start, start + _LINE_HASH_CHARS)
        if end == -1:
            end = min(len(physical), start + _LINE_HASH_CHARS)
        line = physical[start:end].strip().encode("utf-8", "surrogatepass")
        column = position - start + 1
        if self.raw_text is not None:
            # Columns count characters of the RAW file (controls included).
            raw_position = _map_back(self.raw_points, self.raw_totals, position)
            if self._raw_newlines is None:
                self._raw_newlines = LineIndex(self.raw_text)
            raw_start = self._raw_newlines.line_start(raw_position)
            column = raw_position - raw_start + 1
        return index + 1, column, hashlib.sha256(line).hexdigest()


_NEWLINE = re.compile("\n")
_SEMICOLON = re.compile(";")
# Shell quoting the DETECTION text undoes (with an offset map back to the
# physical text for line numbers and hashes):
#   - line continuations (backslash + newline) are removed, so
#     `ba\<newline>sh` reads `bash`;
#   - a backslash, or one / two quote characters, between two letters or
#     digits are removed (`c\url`, `c''url`, `cu"r"l`, `s'h'`);
#   - bash ANSI-C strings are decoded in place (`$'\x73h'` reads `sh`);
#   - whitespace runs collapse: blanks to one space, a run holding line
#     breaks to one line break (indentation and blank lines go). Every
#     bounded `\s{1,N}` in the patterns therefore sees at most one blank, so
#     padding cannot push two tokens apart.
_ELIDED = re.compile(r"\$'|\\\r?\n|\\|''|\"\"|['\"]|[ \t\r\n]{2}")
_BLANKS = frozenset(" \t\r\n")


class _NextChar:
    """First index >= pos holding a character, cached: the detection pass
    moves forward, so repeated searches cost one scan of the text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.cache: dict[str, tuple[int, int]] = {}

    def __call__(self, char: str, pos: int) -> int:
        cached = self.cache.get(char)
        if cached is not None and cached[0] <= pos <= cached[1]:
            return cached[1]
        found = self.text.find(char, pos)
        found = len(self.text) if found == -1 else found
        self.cache[char] = (pos, found)
        return found


def _ansi_c_close(text: str, start: int, next_char: _NextChar | None = None) -> int:
    """Index of the quote closing ``$'`` at ``start`` (same line, however
    long), or -1."""
    next_char = next_char or _NextChar(text)
    stop = next_char("\n", start + 2)
    close = next_char("'", start + 2)
    while close < stop and text[close - 1] == "\\" and text[close - 2 : close] != "\\\\":
        close = next_char("'", close + 1)
    return close if close < stop else -1


# Shells and wrappers whose `-c` / `-S` argument is a command string they run.
_SHELL_NAMES = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "fish", "ash", "mksh", "yash", "csh", "tcsh"}
)
# Command strings inside command strings are re-scanned this deep.
_MAX_STRING_DEPTH = 4

# Characters around a word: blanks, separators, redirections, parentheses.
_WORD_EDGE_CHARS = " \t\r\n;|&()<>"
_WORD_EDGES = frozenset(_WORD_EDGE_CHARS)

# A rewrite of a member (control / invisible removal, the detection text)
# makes at most this many edits; past it the scan fails closed with
# ``hidden_content.analysis_budget`` instead of holding an offset map of
# millions of entries.
MAX_EDITS = 1 << 20


class _AnalysisExhausted(Exception):
    """The analysis budget of one member is spent (too many match positions,
    comment spans or rewrite edits): the member is reported
    ``hidden_content.analysis_budget`` (critical) and its analysis stops."""

    def __init__(self, position: int) -> None:
        super().__init__(position)
        self.position = position


class _Edits:
    """Output pieces and offset map of a text rewrite in bounded memory:
    pieces are joined every few thousand, the map is two int64 arrays, and
    ``mark`` reports when MAX_EDITS is reached."""

    _JOIN_EVERY = 4096

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.pieces: list[str] = []
        self.points: array[int] = array("q")
        self.totals: array[int] = array("q")

    def piece(self, value: str) -> None:
        self.pieces.append(value)
        if len(self.pieces) >= self._JOIN_EVERY:
            self.chunks.append("".join(self.pieces))
            self.pieces = []

    def mark(self, point: int, total: int) -> bool:
        """Record one edit; False once the edit budget is spent."""
        self.points.append(point)
        self.totals.append(total)
        return len(self.points) < MAX_EDITS

    def text(self) -> str:
        self.chunks.append("".join(self.pieces))
        self.pieces = []
        return "".join(self.chunks)


def _requote(value: str) -> str:
    """``value`` as one shell word: single-quoted, or double-quoted with the
    double-quote specials escaped when it holds a single quote."""
    if "'" not in value:
        return "'" + value + "'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return '"' + escaped + '"'


def _detection_text(text: str) -> tuple[str, Sequence[int], Sequence[int]]:
    """(detection text, points, totals). ``points[i]`` is the detection
    offset just after the i-th edit and ``totals[i]`` the characters removed
    up to and including it, so physical = detection + totals[k - 1] where k
    is the number of points <= the detection offset. One linear pass."""
    if (
        "\\" not in text and "'" not in text and '"' not in text and "\t" not in text and "\r" not in text
        and "  " not in text and "\n\n" not in text and " \n" not in text and "\n " not in text
    ):
        return text, [], []
    next_char = _NextChar(text)
    edits = _Edits()
    removed = 0
    last = 0
    length = len(text)
    # Start of the word holding the current position (see the option-word
    # test below); edges are searched incrementally, so this stays linear.
    word_start = 0
    edges_searched = 0
    for match in _ELIDED.finditer(text):
        start, end = match.span()
        if start < last:
            continue  # inside an ANSI-C string already decoded
        token = match.group(0)
        replacement = ""
        if token[0] in _BLANKS:
            end = start
            while end < length and text[end] in _BLANKS:
                end += 1
            run = text[start:end]
            replacement = "\n" if "\n" in run else " "
            if run == replacement:
                continue
        elif token == "$'":
            close = _ansi_c_close(text, start, next_char)
            if close == -1:
                continue
            replacement = _ansi_c(text[start + 2 : close])
            if "\n" in replacement or not replacement.isprintable():
                continue
            end = close + 1
            before = text[start - 1] if start else " "
            after = text[end] if end < length else " "
            if before in _WORD_EDGES and after in _WORD_EDGES:
                # A whole word (`bash -c $'...'`, `<<< $'...'`): it stays ONE
                # quoted word, so a command string keeps its boundaries and
                # is re-parsed like any other. Inside a word (`c$'\x75'rl`)
                # the decoded letters join it.
                replacement = _requote(replacement)
        elif not token.endswith("\n"):
            if token in ("''", '""'):
                # A maximal run of empty quote pairs (c, 4 quotes, u) is one
                # elision: removing pairs one at a time would leave each pair
                # next to another quote, never between two letters.
                while text[end : end + 2] in ("''", '""'):
                    end += 2
            before = text[start - 1] if start else ""
            after = text[end] if end < length else ""
            if not (before.isascii() and before.isalnum() and after.isascii() and after.isalnum()):
                continue
            if token in ("'", '"'):
                # A quote opening the value attached to a short option
                # (`su -c'echo hi'`, `env -S"..."`) keeps its word boundary:
                # removing it would glue the option letter onto the value's
                # first word. Command names never start with `-`, so the
                # elision that matters (`c'u'rl`) is unaffected.
                cut = max(text.rfind(edge, edges_searched, start) for edge in _WORD_EDGE_CHARS)
                if cut != -1:
                    word_start = cut + 1
                edges_searched = start
                if text[word_start] == "-":
                    continue
        edits.piece(text[last:start])
        edits.piece(replacement)
        detection_start = start - removed
        removed += (end - start) - len(replacement)
        if not edits.mark(detection_start + len(replacement), removed):
            raise _AnalysisExhausted(detection_start)
        last = end
    if not edits.points:
        return text, [], []
    edits.piece(text[last:])
    return edits.text(), edits.points, edits.totals


# The ONLY piped-interpreter form treated as data, not code: Python running
# a known stdin-filtering standard-library module. `-c`, `-e`, `-i`, `-`,
# `-n`/`-p` loops and every other form can execute what was downloaded.
_DATA_ONLY_MODULES = frozenset({"json.tool", "json"})

# ---- shell command words (quotes, escapes, wrappers) ------------------------
_MAX_DEPTH = 4  # nested command strings (su -c "env -S 'sudo sh'") re-parsed
_WORD_BREAKS = frozenset("|;&\n()<>`")


@dataclass(frozen=True)
class _Wrapper:
    """A command that runs the command after it.

    short_values / long_values: options taking a separate value argument.
    positionals: leading positional arguments before the command
    (``timeout 5 sh``, ``chroot /root sh``, ``flock FILE sh``).
    command_strings: options whose value IS a command (``su -c STRING``,
    ``env -S STRING``), re-parsed recursively.
    shell_flags: options that start a shell reading stdin when no command
    follows (``sudo -s``, ``doas -s``).
    shell_without_command: with no command at all it is a shell reading
    stdin (``su``).
    short_flags / long_flags: options that take no value (``env -i``,
    ``env --null``); unlike an unrecognized option they are known not to
    consume the next word.
    fail_closed: when an unrecognized option may consume a value, the head
    is unknown rather than mis-parsed (``env`` has many value options)."""

    short_values: frozenset[str] = frozenset()
    long_values: frozenset[str] = frozenset()
    positionals: int = 0
    command_strings: frozenset[str] = frozenset()
    shell_flags: frozenset[str] = frozenset()
    shell_without_command: bool = False
    short_flags: frozenset[str] = frozenset()
    long_flags: frozenset[str] = frozenset()
    fail_closed: bool = False


def _w(
    short: str = "",
    long: tuple[str, ...] = (),
    *,
    positionals: int = 0,
    command_strings: tuple[str, ...] = (),
    shell_flags: str = "",
    shell_without_command: bool = False,
    short_flags: str = "",
    long_flags: tuple[str, ...] = (),
    fail_closed: bool = False,
) -> _Wrapper:
    return _Wrapper(
        frozenset(short), frozenset(long), positionals, frozenset(command_strings),
        frozenset(shell_flags), shell_without_command,
        frozenset(short_flags), frozenset(long_flags), fail_closed,
    )


_WRAPPERS: dict[str, _Wrapper] = {
    "sudo": _w(
        "ugChprtUDT",
        ("--user", "--group", "--close-from", "--host", "--prompt", "--role", "--type",
         "--other-user", "--chdir", "--command-timeout"),
        shell_flags="si",
    ),
    "doas": _w("uC", shell_flags="s"),
    "pkexec": _w("", ("--user",)),
    "su": _w(
        "sgG", ("--shell", "--group", "--supp-group"), positionals=1,
        command_strings=("c", "--command", "--session-command"), shell_without_command=True,
    ),
    "runuser": _w(
        "ugGsl", ("--user", "--group", "--supp-group", "--shell"),
        command_strings=("c", "--command", "--session-command"),
    ),
    "env": _w(
        "uCaP", ("--unset", "--chdir", "--argv0"), command_strings=("S", "--split-string"),
        short_flags="0iv", long_flags=("--ignore-environment", "--null", "--debug"),
        fail_closed=True,
    ),
    "nohup": _w(),
    # ``coproc CMD``: runs CMD with its stdout on a descriptor the script reads.
    "coproc": _w(),
    "setsid": _w(),
    "exec": _w("a"),
    "command": _w(),
    "builtin": _w(),
    "time": _w("fo", ("--format", "--output")),
    "nice": _w("n", ("--adjustment",)),
    "ionice": _w("cnp", ("--class", "--classdata", "--pid", "--pgid", "--uid")),
    "chrt": _w("", (), positionals=1),
    "taskset": _w("", (), positionals=1),
    "timeout": _w("sk", ("--signal", "--kill-after"), positionals=1),
    "chroot": _w("", ("--userspec", "--groups"), positionals=1),
    "flock": _w("wE", ("--timeout", "--conflict-exit-code"), positionals=1,
                command_strings=("c", "--command")),
    "unshare": _w("", ("--propagation", "--setgroups", "--map-user", "--map-group")),
    "caffeinate": _w("tw"),
    # Multicall binaries: the next word is the applet (`busybox sh -c ...`).
    "busybox": _w(),
    "toybox": _w(),
    "stdbuf": _w("ioe", ("--input", "--output", "--error")),
    "xargs": _w("ILnPsdEa", ("--replace", "--max-args", "--max-procs", "--delimiter")),
    "script": _w("ETIOB", ("--timing", "--log-in", "--log-out", "--log-io"), positionals=1,
                 command_strings=("c", "--command")),
}

# _resolve_at returns this index when a fail-closed wrapper sees an option it
# does not recognize (and so cannot know whether the next word is a value or
# the command): the head is unknown. Distinct from -1 (a command string /
# shell wrapper decided it) and any valid word index.
_FAIL_CLOSED = -2
_FAIL_CLOSED_WRAPPERS = frozenset(
    name for name, wrapper in _WRAPPERS.items() if wrapper.fail_closed
)


def _is_assignment(word: str) -> bool:
    """``NAME=value`` / ``NAME+=value`` with a shell name of any length."""
    name, equals, _value = word.partition("=")
    if not equals:
        return False
    if name.endswith("+"):
        name = name[:-1]
    return _is_shell_name(name)


def _is_shell_name(name: str) -> bool:
    if not name or not name.isascii() or name[0].isdigit():
        return False
    return name.replace("_", "a").isalnum()


_SOURCE_WORDS = frozenset({"source", ".", "eval", "iex", "invoke-expression"})
# Concrete interpreter names, for glob / brace command names (`/???/b??h`).
_INTERPRETER_WORDS = (
    "sh", "bash", "zsh", "dash", "ksh", "fish", "ash", "mksh", "yash", "csh", "tcsh",
    "python", "python2", "python3", "pypy", "pypy3", "perl", "ruby", "node", "php",
    "pwsh", "powershell", "iex", "lua", "luajit", "tclsh", "osascript",
)
_ANSI_C_ESCAPE = re.compile(r"\\(x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|[0-7]{1,3}|.)")
_ANSI_C_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b",
                  "f": "\f", "v": "\v", "\\": "\\", "'": "'", '"': '"'}


def _ansi_c(body: str) -> str:
    """Decode a bash ``$'...'`` string (``$'\\x73h'`` is ``sh``)."""

    def decode(match: re.Match[str]) -> str:
        token = match.group(1)
        if token[0] in "xu" and len(token) > 1:
            return chr(int(token[1:], 16))
        if token[0] in "01234567":
            return chr(int(token, 8) % 256)
        return _ANSI_C_SIMPLE.get(token, token)

    return _ANSI_C_ESCAPE.sub(decode, body)


def _skip_to_next_word(text: str, start: int) -> int:
    """Skip the blanks and line breaks a shell allows between `|`, `&&`,
    `||` (or `$(` / `<(`) and the command that follows (any number)."""
    index = start
    length = len(text)
    while index < length and text[index] in " \t\r\n":
        index += 1
    return index


def _command_words(text: str, start: int, limit: int | None = None) -> tuple[list[str], int]:
    words, _heads, end = _command_words_and_heads(text, start, limit)
    return words, end


def _command_words_and_heads(
    text: str, start: int, limit: int | None = None
) -> tuple[list[str], list[str], int]:
    """Shell words of the command starting at ``start``: quotes removed
    (including bash ``$'...'`` ANSI-C strings, decoded), backslash escapes
    resolved, stopping at an unquoted separator, a newline or ``limit``
    characters (default: none). Returns (words, heads, end).

    ``heads`` is the same list with each word cut where a quote opened in the
    MIDDLE of it: for a pipe that is itself inside a quoted string
    (``echo 'curl URL | sh' >> ~/.bashrc``) the closing quote really ends
    the command word, and the scanner checks both readings."""
    words: list[str] = []
    heads: list[str] = []
    head: str | None = None
    mid_quote_at: int | None = None
    current: list[str] = []
    in_word = False
    index = start
    end = len(text) if limit is None else min(len(text), start + limit)
    next_newline = -1
    while index < end:
        char = text[index]
        ansi = char == "$" and text[index + 1 : index + 2] in ("'", '"')
        if char in "'\"" or ansi:
            if in_word and head is None:
                head = "".join(current)
                if mid_quote_at is None:
                    mid_quote_at = index
            in_word = True
            if ansi:
                index += 1
                char = text[index]
            # The quoted span is located with str.find (C speed), so a
            # pathological run of quotes stays linear.
            if next_newline <= index:
                next_newline = text.find("\n", index + 1)
                next_newline = len(text) if next_newline == -1 else next_newline
            stop = min(next_newline, end)
            close = text.find(char, index + 1, stop)
            while close != -1 and text[close - 1] == "\\" and (char == '"' or ansi):
                close = text.find(char, close + 1, stop)
            body = text[index + 1 : close if close != -1 else stop]
            if ansi and char == "'":
                body = _ansi_c(body)
            elif char == '"':
                body = body.replace('\\"', '"')
            current.append(body)
            if close == -1:
                index = stop
                break
            index = close + 1
            continue
        elif char == "\\":
            if index + 1 < end:
                index += 1
                if text[index] != "\n":
                    current.append(text[index])
                    in_word = True
        elif char in " \t\r":
            if in_word:
                words.append("".join(current))
                heads.append(head if head is not None else words[-1])
                current = []
                head = None
                in_word = False
        elif char in _WORD_BREAKS:
            break
        else:
            current.append(char)
            in_word = True
        index += 1
    if in_word:
        words.append("".join(current))
        heads.append(head if head is not None else words[-1])
    # The end reported to callers (who skip pipes before it as "quoted") never
    # passes a mid-word quote: what follows may be outside any real quote.
    return words, heads, index if mid_quote_at is None else mid_quote_at


def resolve_command(
    words: list[str], depth: int = 0
) -> tuple[str, list[str]] | None:
    """(command name, remaining words) after skipping wrapper commands
    (sudo, env, timeout, chroot, su -c ...) with their options, positional
    arguments and ``VAR=value`` assignments; command strings (``su -c``,
    ``env -S``) are parsed recursively. The name has any path prefix
    removed and is casefolded. A wrapper that is itself a shell reading
    stdin (``sudo -s``, bare ``su``) resolves to ``sh``."""
    index, special = _resolve_at(words, 0, depth)
    if index >= 0:
        return words[index].rsplit("/", 1)[-1].casefold(), words[index + 1 :]
    return special


def _resolve_at(
    words: list[str], start: int, depth: int = 0
) -> tuple[int, tuple[str, list[str]] | None]:
    """resolve_command from ``start`` without copying: (index of the command
    word, None), or (-1, resolved) when a command string or a shell wrapper
    decides it, or (-1, None) when nothing runs, or (_FAIL_CLOSED, None) when
    a fail-closed wrapper's unrecognized option may consume a value."""
    index = start
    while index < len(words):
        word = words[index]
        if _is_assignment(word):
            index += 1
            continue
        name = word.rsplit("/", 1)[-1].casefold()
        wrapper = _WRAPPERS.get(name)
        if wrapper is None:
            return index, None
        index += 1
        positionals = wrapper.positionals
        shell = False
        while index < len(words):
            option = words[index]
            if option == "--":
                index += 1
                break
            if _is_assignment(option) and positionals == wrapper.positionals:
                index += 1
                continue
            if option.startswith("-") and option != "-":
                index += 1
                if option.startswith("--"):
                    key, _, attached = option.partition("=")
                    value = attached if attached else None
                    if key in wrapper.command_strings:
                        if value is None and index < len(words):
                            value = words[index]
                            index += 1
                        return -1, _resolve_string(value or "", depth)
                    if key in wrapper.long_values:
                        if not attached:
                            index += 1
                        continue
                    if key in wrapper.long_flags:
                        continue
                    if wrapper.fail_closed:
                        return _FAIL_CLOSED, None
                    continue
                letters = option[1:]
                if letters.isdigit():
                    continue  # nice -10
                for position, letter in enumerate(letters):
                    if letter in wrapper.shell_flags:
                        shell = True
                        continue
                    if letter in wrapper.command_strings:
                        attached = letters[position + 1 :]
                        value = attached or (words[index] if index < len(words) else "")
                        return -1, _resolve_string(value, depth)
                    if letter in wrapper.short_values:
                        if position == len(letters) - 1:
                            index += 1  # the value is the next word
                        break  # the rest of the cluster is the value
                    if letter in wrapper.short_flags:
                        continue
                    if wrapper.fail_closed:
                        return _FAIL_CLOSED, None
                    continue
                continue
            if positionals > 0:
                positionals -= 1
                index += 1
                continue
            break
        if index >= len(words):
            if shell or wrapper.shell_without_command:
                return -1, ("sh", [])
            return -1, None
    return -1, None


_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O"})


def _unknowable_head(word: str) -> bool:
    """A command name that cannot be known statically: an expansion, a
    substitution or a glob."""
    return "$" in word or "`" in word or SUB_MARK in word or any(char in word for char in "*?[")
# Long shell options that take the next word as their value.
_SHELL_LONG_VALUE_OPTIONS = frozenset({"--rcfile", "--init-file"})


def _command_string_at(
    words: list[str], names: frozenset[str] = frozenset()
) -> tuple[int, str] | None:
    """(index of the word holding it, the command string) a shell, wrapper
    or string evaluator RUNS, found by following the real command head and
    its wrapper chain only (never an ordinary argument that happens to say
    ``bash``):

    * a shell (sh, bash, ...) with ``-c`` among its leading options (also in
      a cluster: ``-lc``, ``-ec``): the first argument after the options;
    * a wrapper's command-string option (``su -c``, ``env -S``, ``flock -c``,
      ``script -c``, ``runuser -c`` ...): separate (``-c STR``), attached
      short (``-cSTR``, ``-SSTR``) or long with ``=`` (``--command=STR``,
      ``--split-string=STR``);
    * ``eval`` / ``iex`` / ``invoke-expression``: its arguments, joined.

    The string is the words' decoded value, however quoted or escaped. A
    head that cannot be known statically - an expansion, a substitution, a
    glob such as ``/???/b??h``, or one of ``names`` (an alias / function
    this member defines, any case) - is read as a shell: its ``-c`` string
    is analyzed rather than skipped."""
    return _command_string_plain(words, names)


_STRING_EVALUATORS = frozenset({"eval", "iex", "invoke-expression"})


def _command_string_plain(words: list[str], names: frozenset[str] = frozenset()) -> tuple[int, str] | None:
    index = 0
    shift = 0
    while index < len(words):
        word = words[index]
        if _is_assignment(word):
            index += 1
            continue
        name = word.rsplit("/", 1)[-1].casefold()
        if name in _STRING_EVALUATORS:
            if index + 1 < len(words):
                return index + 1, " ".join(words[index + 1 :])
            return None
        if name in _SHELL_NAMES or _unknowable_head(word) or word.casefold() in names:
            position = index + 1
            takes_string = False
            while position < len(words):
                option = words[position]
                if option == "--":
                    position += 1
                    break
                if option in _SHELL_VALUE_OPTIONS or option in _SHELL_LONG_VALUE_OPTIONS:
                    position += 2
                    continue
                if len(option) > 1 and option[0] in "-+" and not option.startswith("--"):
                    if option[0] == "-" and "c" in option[1:]:
                        takes_string = True
                    position += 1
                    continue
                if option.startswith("--"):
                    position += 1  # --norc, --login ...
                    continue
                break
            if takes_string and position < len(words):
                return position - shift, words[position]
            return None
        wrapper = _WRAPPERS.get(name)
        if wrapper is None:
            return None
        index += 1
        positionals = wrapper.positionals
        while index < len(words):
            option = words[index]
            if option == "--":
                index += 1
                break
            if _is_assignment(option) and positionals == wrapper.positionals:
                index += 1
                continue
            if option.startswith("-") and option != "-":
                index += 1
                if option.startswith("--"):
                    key, equals, attached = option.partition("=")
                    if key in wrapper.command_strings:
                        if equals:
                            return index - 1 - shift, attached
                        return (index - shift, words[index]) if index < len(words) else None
                    if key in wrapper.long_values and not equals:
                        index += 1
                    continue
                letters = option[1:]
                if letters.isdigit():
                    continue
                for position, letter in enumerate(letters):
                    if letter in wrapper.command_strings:
                        attached = letters[position + 1 :]
                        if attached:
                            return index - 1 - shift, attached
                        return (index - shift, words[index]) if index < len(words) else None
                    if letter in wrapper.short_values:
                        if position == len(letters) - 1:
                            index += 1
                        break
                continue
            if positionals > 0:
                positionals -= 1
                index += 1
                continue
            break
        # the wrapped command continues the chain at ``index``
    return None


def _resolve_string(value: str, depth: int) -> tuple[str, list[str]] | None:
    if SUB_MARK in value:
        # `su -c "$(curl URL)"`, `env -S "$(...)"`: the command string IS a
        # substitution's output - a shell runs it, like `sh -c "$(...)"`.
        return "sh", ["-c", value]
    if depth >= _MAX_DEPTH:
        # Nested past what is resolved: what runs is unknown, so it is
        # treated as a shell (fail closed), never as "nothing runs".
        return "sh", []
    return resolve_command(_command_words(value, 0)[0], depth + 1)


def _has_brace_expansion(word: str) -> bool:
    """A syntactically valid brace expansion (``{a,b}`` or ``{x..y}``, nested
    or repeated in one word): the shell decides the word's value at run time,
    so a command name holding one cannot be known statically. One linear
    scan; the expansion is NEVER materialized (a long prefix times many
    alternatives would otherwise copy the prefix into every candidate, so a
    sub-megabyte statement could request gigabytes before any budget is
    charged). ``${...}`` (a parameter expansion) is not a brace group."""
    depth = 0
    open_at = 0
    for index, char in enumerate(word):
        if char == "{" and word[index - 1 : index] != "$":
            if depth == 0:
                open_at = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and ("," in word[open_at:index] or ".." in word[open_at:index]):
                return True
    return False


def _is_interpreter_name(name: str, aliases: Container[str] = frozenset()) -> bool:
    """An interpreter, or a command name that cannot be known statically and
    therefore must be assumed to be one: a parameter or command expansion
    (``$SHELL``, ``${x}sh``), a brace expansion (``{ba,}sh``, ``b{a..a}sh`` -
    sequence, nested and multiple expansions included), a glob matching an
    interpreter (``/???/b??h``); or an alias / function this file defines as
    an interpreter."""
    if name in aliases:
        return True
    if "$" in name or "`" in name or SUB_MARK in name:
        return True
    if _has_brace_expansion(name):
        return True
    base = name.rsplit("/", 1)[-1]
    if _INTERPRETER_NAME.fullmatch(base):
        return True
    return any(char in base for char in "*?[") and any(
        fnmatch.fnmatchcase(word, base) for word in _INTERPRETER_WORDS
    )


def is_data_only_invocation(name: str, rest: list[str]) -> bool:
    """The one interpreter invocation that reads data rather than running it:
    Python running a stdin/file filtering standard-library module
    (``python -m json.tool``). Shared by every path that judges a runner."""
    return (
        name.startswith("python")
        and rest[:1] == ["-m"]
        and len(rest) > 1
        and rest[1] in _DATA_ONLY_MODULES
    )


def _runner(
    words: list[str], aliases: Container[str] = frozenset()
) -> tuple[str, list[str]] | None:
    """THE runner decision, used by every path (pipe, output and input
    process substitution, command substitution / here-string, here-document,
    file-descriptor read, download-then-run, aliases): after wrapper
    resolution, a command that executes what it is given - an interpreter
    (or an unknowable command name, see _is_interpreter_name) that is not a
    data-only invocation, or a source word (source, ., eval, iex,
    invoke-expression), or one of ``aliases`` - the alias / function names
    the member defines, casefolded (over-approximated: any of them may run
    what it is given). Returns (name, remaining words) or None."""
    resolved = resolve_command(words)
    if resolved is None:
        return None
    name, rest = resolved
    if name in _SOURCE_WORDS or name in aliases:
        return name, rest
    if _is_interpreter_name(name, aliases) and not is_data_only_invocation(name, rest):
        return name, rest
    return None


def _first_command_word(words: list[str]) -> str:
    """The command word itself (wrappers skipped), WITHOUT stripping its path:
    ``./tool`` stays ``./tool``."""
    resolved = resolve_command(words)
    if resolved is None:
        return ""
    count = len(words) - len(resolved[1])
    return words[count - 1] if count >= 1 else ""


# Aliases / functions a file defines, resolved to whether they run an interpreter.
_ALIAS_DEF = re.compile(r"\balias\s", _I)
_FUNCTION_KEYWORD = re.compile(r"\bfunction\s", _I)
_FUNCTION_PARENS = re.compile(r"\(\s{0,4}\)\s{0,8}\{")
# A bash function name may hold punctuation (``foo+bar()``, ``foo/bar()``,
# ``foo%bar()``): the whole shell word before ``()`` is the name, bounded by
# the same characters that end a shell word (see _SHELL_TOKEN_WORD).
_NAME_BREAKS = frozenset(" \t\r\n;|&()<>`'\"")

_PREFILTERS[_ALIAS_DEF] = (("alias",),)
_PREFILTERS[_FUNCTION_KEYWORD] = (("function",),)
_PREFILTERS[_FUNCTION_PARENS] = (("(",), ("{",))


def _name_before(text: str, end: int) -> str:
    """The name (any length) ending at ``end``, one optional blank skipped."""
    if end > 0 and text[end - 1] in " \t":
        end -= 1
    start = end
    while start > 0 and text[start - 1] not in _NAME_BREAKS:
        start -= 1
    return text[start:end]


# Shell indirection is deliberately OVER-approximated, not emulated: every
# alias and function name a member defines, gathered from ANY occurrence
# (no ordering, scoping, removal or case semantics), is an UNKNOWN command -
# it may be a fetcher, a decoder or a runner - as is a command name that is
# an expansion (``$F``, ``${F}``), a substitution or a glob, and ``eval`` /
# ``iex`` of anything but literal text. Precise emulation of aliases,
# functions and variables does not converge; the over-approximation is
# cheap, linear and fail-closed, and the quarantines it causes are reviewed
# by people (D3).
_DEFINITION_LIMIT = 20_000  # alias / function definitions per member
# Languages whose own `function NAME(`, `$x` or `*` text is not shell
# indirection: unknown-command analysis does not apply to them.
_NON_SHELL_SUFFIXES = frozenset(
    {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".py", ".rb", ".pl", ".php", ".lua", ".go",
     ".rs", ".java", ".kt", ".swift", ".c", ".h", ".cpp", ".cs", ".html", ".htm", ".css", ".svg",
     ".xml"}
)


_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "ash", "mksh", "yash"})
_SHELL_RUN_WORD = re.compile(r"\b(?:sh|bash|zsh|dash|ksh|ash|mksh|yash|source)\b", _I)
_PREFILTERS[_SHELL_RUN_WORD] = (("sh", "source"),)
_RUN_WORD_SPLIT = re.compile(r"[\s\"';|&()<>=,\[\]{}]")


_ENV_FLAG_SHORT = frozenset("0iv")  # env short options without a value
_ENV_VALUE_SHORT = frozenset("uCaP")  # env short options taking a value
_ENV_FLAG_LONG = frozenset({"--ignore-environment", "--null", "--debug"})
_ENV_VALUE_LONG = frozenset({"--unset", "--chdir", "--argv0"})


def _has_shell_shebang(text: str) -> bool:
    """``#!/bin/sh``, ``#!/usr/bin/bash``, ``#!/usr/bin/env [-S ...]
    [VAR=x] zsh``. The ``env`` option arity is parsed (``-u NAME``,
    ``-C DIR``, ``--unset=NAME``, attached ``-uNAME`` and ``-S STRING`` forms)
    so an option's value is never mistaken for the interpreter. An
    unrecognized option means the file is treated as shell: when in doubt,
    it is one."""
    if not text.startswith("#!"):
        return False
    end = text.find("\n", 0, 4096)
    words = text[2 : end if end != -1 else 4096].split()
    if not words:
        return False
    if words[0].rsplit("/", 1)[-1] != "env":
        return words[0].rsplit("/", 1)[-1] in _SHELLS
    return _env_invokes_a_shell(words[1:])


def _split_env_string(value: str) -> list[str]:
    """The words of an ``env -S`` string: quotes and escapes are processed,
    as env does, and the result is flattened on whitespace (a quoted span
    holding spaces still names a command head to check). A malformed string
    falls back to plain whitespace splits."""
    try:
        tokens = shlex.split(value)
    except ValueError:
        tokens = value.split()
    return [piece for token in tokens for piece in token.split()]


def _balanced_quotes(value: str) -> bool:
    return value.count("'") % 2 == 0 and value.count('"') % 2 == 0


def _env_invokes_a_shell(words: list[str], depth: int = 0) -> bool:
    """The command ``env WORDS...`` runs a shell. ``-S`` / ``--split-string``
    restarts the parse over the words of its string; an option shape this
    does not recognize fails toward "is a shell". The kernel hands the whole
    shebang tail to ``env`` as ONE argument, so a split string that quotes
    across words is the joined remainder of the line."""
    if depth >= 8:
        return True

    def restart(string: str, index: int) -> bool:
        if not string.strip():
            return True
        return _env_invokes_a_shell(_split_env_string(string) + words[index:], depth + 1)

    def split_string(attached: str, index: int) -> bool:
        if attached and _balanced_quotes(attached):
            return restart(attached, index + 1)
        string = (attached + " " if attached else "") + " ".join(words[index + 1 :])
        return restart(string, len(words))

    index = 0
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if _is_assignment(word) or word == "-":
            index += 1
            continue
        if not word.startswith("-"):
            break
        if word.startswith("--"):
            name, _, attached = word.partition("=")
            if name == "--split-string":
                return split_string(attached, index)
            if name in _ENV_FLAG_LONG:
                index += 1
                continue
            if name in _ENV_VALUE_LONG:
                index += 1 if attached else 2
                continue
            return True
        # A short-option cluster: 0/i/v are flags; u/C/a take a value
        # (attached ends the cluster, otherwise the next word is the value);
        # S restarts the parse over its split string.
        cluster = word[1:]
        consumes_next = False
        for position, char in enumerate(cluster):
            if char in _ENV_FLAG_SHORT:
                continue
            if char in _ENV_VALUE_SHORT:
                consumes_next = position == len(cluster) - 1
                break
            if char == "S":
                return split_string(cluster[position + 1 :], index)
            return True
        index += 2 if consumes_next else 1
    return index < len(words) and words[index].rsplit("/", 1)[-1] in _SHELLS


def _members_run_by_a_shell(members: list[tuple[str, str]]) -> frozenset[str]:
    """Members a hook, script or config runs through a shell (``bash
    ${CLAUDE_PLUGIN_ROOT}/x.py``, ``source lib.txt``): after a shell word on
    the same line, any word whose basename names a member. Discovery runs
    over the detection text (line continuations joined, quoting normalized)
    so a run split across a ``\\``-newline still matches. Coarse on purpose;
    one pass per line."""
    by_name: dict[str, list[str]] = {}
    for path, _text in members:
        by_name.setdefault(PurePosixPath(path).name, []).append(path)
    found: set[str] = set()
    for path, text in members:
        if classify_path(path) not in EXEC_CONTEXTS:
            continue
        try:
            haystack = _detection_text(text)[0]
        except _AnalysisExhausted:
            haystack = text
        done = -1
        for match in _SHELL_RUN_WORD.finditer(haystack):
            if match.start() < done:
                continue
            end = haystack.find("\n", match.end())
            end = len(haystack) if end == -1 else end
            done = end
            for word in _RUN_WORD_SPLIT.split(haystack[match.end() : end]):
                name = word.rsplit("/", 1)[-1]
                if name in by_name:
                    found.update(by_name[name])
    return frozenset(found)


def _indirection_applies(scan: _FileScan) -> bool:
    """Shell indirection analysis applies to every member except one in a
    language whose own ``function NAME(``, ``$x`` or ``*`` text is not shell
    (JavaScript, Python, HTML ...) - unless it is shell after all: a shell
    shebang, run by a shell (a hook / script), or a command string."""
    if scan.shell or _has_shell_shebang(scan.text):
        return True
    return PurePosixPath(scan.path).suffix.casefold() not in _NON_SHELL_SUFFIXES


def _shell_file(scan: _FileScan) -> bool:
    """The whole member is shell code (not prose that may hold some)."""
    return scan.shell or scan.executable or _has_shell_shebang(scan.text)


_FENCE = re.compile(r"(?m)^[ \t]{0,3}(?:```|~~~)[ \t]{0,8}([A-Za-z0-9_+-]{0,32})")
_SHELL_FENCE_LANGUAGES = frozenset(
    {"sh", "bash", "shell", "zsh", "ksh", "dash", "console", "shell-session", "sh-session",
     "shellsession", "terminal", "bash-session"}
)


def _prose_shell_context(scan: _FileScan) -> None:
    """Spans of shell content inside prose (fenced blocks marked as shell,
    indented command lines outside fences) and the starts of Markdown table
    lines. One linear pass, cached on the scan."""
    text = scan.text
    spans: list[tuple[int, int]] = []
    tables: list[int] = []
    fence_open: int | None = None
    fence_shell = False
    position = 0
    length = len(text)
    while position < length:
        end = text.find("\n", position)
        end = length if end == -1 else end
        fence = _FENCE.match(text, position, end)
        if fence is not None:
            if fence_open is None:
                fence_open = end
                fence_shell = fence.group(1).casefold() in _SHELL_FENCE_LANGUAGES
            else:
                if fence_shell:
                    spans.append((fence_open, position))
                fence_open = None
        elif fence_open is None:
            line = text[position:end]
            if line.startswith(("    ", "\t")) and line.strip():
                spans.append((position, end))
            elif line.lstrip(" \t>").startswith("|"):
                tables.append(position)
        position = end + 1
    if fence_open is not None and fence_shell:
        spans.append((fence_open, length))
    scan.shell_spans = spans
    scan.table_lines = tables


def _context_at(scan: _FileScan, position: int, shell_file: bool) -> str:
    """"shell" (shell code), "table" (a Markdown table line in prose) or
    "prose", for classifying an expansion / glob command name."""
    if shell_file:
        return "shell"
    if scan.shell_spans is None or scan.table_lines is None:
        _prose_shell_context(scan)
    assert scan.shell_spans is not None and scan.table_lines is not None
    index = bisect.bisect_right(scan.shell_spans, (position, len(scan.text) + 1)) - 1
    if index >= 0 and scan.shell_spans[index][0] <= position <= scan.shell_spans[index][1]:
        return "shell"
    line_start = scan.logical_line_start(position)
    index = bisect.bisect_left(scan.table_lines, line_start)
    if index < len(scan.table_lines) and scan.table_lines[index] == line_start:
        return "table"
    return "prose"


def _shell_words(text: str, start: int = 0) -> Iterator[tuple[int, int]]:
    """(start, end) of every unquoted shell word chunk run, of any length:
    bounded regex chunks that touch are merged."""
    current: tuple[int, int] | None = None
    for match in _SHELL_TOKEN_WORD.finditer(text, start):
        if current is not None and current[1] == match.start():
            current = (current[0], match.end())
            continue
        if current is not None:
            yield current
        current = (match.start(), match.end())
    if current is not None:
        yield current


def _shell_word_at(text: str, position: int) -> str:
    """The whole shell word starting at ``position`` (any length)."""
    end = position
    while True:
        match = _SHELL_TOKEN_WORD.match(text, end)
        if match is None:
            return text[position:end]
        end = match.end()


def _indirection_names(scan: _FileScan) -> frozenset[str]:
    """Casefolded alias and function names the member defines anywhere
    (``alias NAME=...``, every pair of one alias command; ``NAME() {``;
    ``function NAME``). One linear pass; every definition is charged against
    a dedicated definition budget (past it: analysis budget, fail closed)."""
    if not _indirection_applies(scan):
        return frozenset()
    text = scan.text
    names: set[str] = set()
    count = 0

    def charge(position: int) -> None:
        nonlocal count
        count += 1
        if count > _DEFINITION_LIMIT:
            raise _AnalysisExhausted(position)
        scan.charge(1, position)  # also an item of the member's budget

    covered = -1
    for match in scan.finditer(_ALIAS_DEF):
        if match.start() < covered:
            continue
        words, covered = _command_words(text, match.end())
        for word in words:
            name, equals, _value = word.partition("=")
            if equals and name:
                charge(match.start())
                names.add(name.casefold())
    shell_file = _shell_file(scan)
    for match in scan.finditer(_FUNCTION_KEYWORD):
        at = _skip_to_next_word(text, match.end())
        token = _shell_word_at(text, at)
        if not token:
            continue
        if not shell_file and "(" not in token and _context_at(scan, match.start(), False) != "shell":
            # Prose ("the enclosing function in `symbol`"): only a real
            # definition shape - the name followed by `{`, `(` or a line end.
            after = at + len(token)
            while after < len(text) and text[after] in " \t":
                after += 1
            if text[after : after + 1] not in ("{", "(", "\n", ""):
                continue
        charge(match.start())
        names.add(token.split("(", 1)[0].casefold())
    for match in scan.finditer(_FUNCTION_PARENS):
        name = _name_before(text, match.start())
        if name:
            charge(match.start())
            names.add(name.casefold())
    names.discard("")
    return frozenset(names)


def _is_unknown_head(words: list[str], names: frozenset[str], context: str = "shell") -> bool:
    """The command's name (after wrappers) cannot be known statically: a
    defined alias / function (any case), an expansion, a substitution or a
    glob (see _unknown_token for ``context``); or ``eval`` / ``iex`` /
    ``invoke-expression`` of anything but literal text (an expansion or a
    substitution; the substitution's body is also scanned on its own)."""
    index = _resolve_at(words, 0)[0]
    if index == _FAIL_CLOSED:
        return True  # a fail-closed wrapper option hides the real head
    if index < 0:
        return False
    head = words[index]
    if head.casefold() in names or _unknown_token(head, context):
        return True
    if head.rsplit("/", 1)[-1].casefold() in _STRING_EVALUATORS:
        return any(_non_literal(word) for word in words[index + 1 :])
    return False


def _non_literal(word: str) -> bool:
    return "$" in word or "`" in word or SUB_MARK in word


def _argument_paths(word: str) -> list[str]:
    """Paths an argument may name: the word itself, or an option's attached
    value (``--output=/tmp/x``, ``-o/tmp/x``, ``-sLo/tmp/x``)."""
    if not word.startswith("-") or word == "-":
        return [word]
    if "=" in word:
        return [word.split("=", 1)[1]]
    body = word.lstrip("-")
    values = [body[1:]] if len(body) > 1 else []  # -oFILE
    for index, char in enumerate(body):
        if not char.isalpha():
            values.append(body[index:])  # -sLo/tmp/x, -o./x
            break
    return values


def _runner_argument(word: str, names: frozenset[str]) -> bool:
    """An argument that names something that runs what it reads: an
    interpreter (also as a glob / brace), ``source`` / ``eval`` / ``iex``, or
    an alias / function the member defines. Expansions are not counted
    (``log "$msg"`` is everyday code); ``.`` is a directory argument."""
    if not word or "$" in word or "`" in word or SUB_MARK in word:
        return False
    base = word.rsplit("/", 1)[-1].casefold()
    if base in names or (base in _SOURCE_WORDS and base != "."):
        return True
    return _is_interpreter_name(word)


# Names a glob command name could stand for and be dangerous.
_STAGE_WORDS = (*_INTERPRETER_WORDS, "curl", "wget", "aria2c", "base64", "base32", "xxd", "uudecode", "openssl")
_VARIABLE_HEAD = re.compile(r"\$\{?[A-Za-z_]")
# Positional / special parameters and indirect expansion: $1 $@ $* $# $? $$
# $! $- $0, ${1}, ${@}, ${!name}.
_PARAMETER_HEAD = re.compile(r"\$\{?[0-9@*#?$!-]")


def _unknown_token(token: str, context: str = "shell") -> bool:
    """A command name that cannot be known statically: a variable expansion
    (``$F``, ``${F}``), a positional / special parameter or indirect
    expansion (``$1``, ``$@``, ``${!name}``), a brace expansion (``{ba,}sh``,
    ``b{a..a}sh`` — the shell decides the name at run time), or a glob that
    could match an interpreter, fetcher or decoder (``/???/c?rl``). The test
    command (``[``) is not. ``context`` is "shell" for shell code, "prose" or
    "table" (a Markdown table line) for prose: there a price (``$5``) is not
    a parameter, and table cells, list markers and balanced emphasis
    (``**bash**``) are not globs."""
    if context == "table":
        # A Markdown table cell is documentation, not a command name: even
        # ``$SHELL`` / ``{ba,z}sh`` there is a literal example, not indirection.
        return False
    if _VARIABLE_HEAD.search(token):
        return True
    param = _PARAMETER_HEAD.search(token)
    if param is not None:
        if context == "shell":
            return True
        # In prose only a whole-word special parameter (``$@``, ``$*``,
        # ``${!x}``) is a runner; ``$5`` is a price.
        if param.start() == 0 and (
            token[1:2] in ("@", "*") or token.startswith(("${!", "${@", "${*"))
        ):
            return True
    if _has_brace_expansion(token):
        return True
    base = token.rsplit("/", 1)[-1]
    if base in ("[", "[[") or not any(char in base for char in "*?["):
        return False
    if context != "shell" and _markdown_markup(base):
        return False
    return any(fnmatch.fnmatchcase(word, base) for word in _STAGE_WORDS)


def _markdown_markup(token: str) -> bool:
    """A list marker (``*``, ``**``) or balanced emphasis (``*x*``,
    ``**x**``, ``***x***``) with no other glob character inside."""
    stripped = token.strip("*")
    if not stripped:
        return True
    leading = len(token) - len(token.lstrip("*"))
    trailing = len(token) - len(token.rstrip("*"))
    return leading == trailing and not any(char in stripped for char in "*?[")


# Download output paths (for linking a later run to the download).
_OUTPUT_REDIRECT = re.compile(r">{1,2}\s{0,4}")
_PATH_STOP = frozenset(" \t\r\n;&|<>")
_OUTPUT_FLAG_END = re.compile(r"-[A-Za-z]{0,6}[oO]")
_OUTPUT_FLAG_ATTACHED = re.compile(r"-[A-Za-z]{0,6}o[^\s-]")
_REMOTE_NAME_FLAG = re.compile(r"-[A-Za-z]{0,6}O[A-Za-z]{1,6}")


def _download_outputs(command: str) -> set[str]:
    """Paths a curl / wget command writes: ``-o FILE``, ``--output FILE``,
    ``-O FILE`` / ``--output-document`` (wget), ``> FILE``, and the URL's
    basename for ``curl -O``. Basenames are included for loose matching."""
    words = _command_words(command, 0)[0]
    outputs: set[str] = set()
    remote_name = False
    url_names: list[str] = []
    fetcher = words[0].casefold() if words else ""
    for position, word in enumerate(words):
        value = words[position + 1] if position + 1 < len(words) else ""
        if word in ("--output", "--output-document") or _OUTPUT_FLAG_END.fullmatch(word):
            if fetcher == "wget" or word[-1] != "O":
                outputs.add(value)  # -o FILE, -sLo FILE, wget -O FILE / -qO FILE
            else:
                remote_name = True  # curl -O / -LO: named after the URL
        elif word.startswith("--output=") or word.startswith("--output-document="):
            outputs.add(word.split("=", 1)[1])
        elif _OUTPUT_FLAG_ATTACHED.match(word) and " " not in word:
            outputs.add(word[word.index("o") + 1 :])  # -oFILE (any length)
        elif word == "--remote-name" or _REMOTE_NAME_FLAG.fullmatch(word):
            remote_name = fetcher == "curl" or remote_name
        elif "://" in word:
            url_names.append(word.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0])
    for match in _OUTPUT_REDIRECT.finditer(command):
        end = match.end()
        while end < len(command) and command[end] not in _PATH_STOP:
            end += 1
        if end > match.end():
            outputs.add(command[match.end() : end])
    if remote_name or fetcher == "wget":
        outputs.update(name for name in url_names if name)
    outputs.discard("")
    outputs.discard("-")
    return outputs | {path.rsplit("/", 1)[-1] for path in outputs}


def _last_before(positions: list[int], position: int, floor: int) -> int | None:
    index = bisect.bisect_left(positions, position) - 1
    if index >= 0 and positions[index] >= floor:
        return positions[index]
    return None


def _any_within(positions: list[int], low: int, high: int) -> bool:
    index = bisect.bisect_left(positions, low)
    return index < len(positions) and positions[index] < high


def _severity(executable: bool, *, exec_sev: str, prose_sev: str) -> str:
    return exec_sev if executable else prose_sev


_QUOTE_STOP = re.compile(r"['\"\\#]")


def _multiline_quotes(scan: _FileScan) -> list[tuple[int, int]]:
    """(opening, closing) offsets of the quoted words in shell code that span
    a line break, in order (computed once per file, charged per span): the
    same quoting the lexer applies in shell code, ``#`` comments at a word
    start skipped."""
    if scan.quote_spans is not None:
        return scan.quote_spans
    text = scan.text
    spans: list[tuple[int, int]] = []
    length = len(text)
    index = 0
    while index < length:
        stop = _QUOTE_STOP.search(text, index)
        if stop is None:
            break
        index = stop.start()
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "#":
            if index == 0 or text[index - 1] in " \t\n;&|(":
                newline = text.find("\n", index)
                index = length if newline == -1 else newline
            else:
                index += 1
            continue
        close = text.find(char, index + 1)
        while close != -1 and char == '"' and text[close - 1] == "\\":
            close = text.find(char, close + 1)
        if close == -1:
            index += 1  # never closed: a literal, as in the lexer
            continue
        if text.find("\n", index, close) != -1:
            scan.charge(1, index)
            spans.append((index, close))
        index = close + 1
    scan.quote_spans = spans
    return spans


def _statement_start(scan: _FileScan, position: int) -> int:
    """Where the statement holding ``position`` begins: its logical line, or
    an earlier line it continues (one ending in ``|``, ``&&``, ``||`` or an
    opening parenthesis, or holding an unclosed ``(`` / ``$(`` / ``<(``) -
    however many lines and characters back. Each line is examined once per
    file (memoized), so this is linear overall."""
    text = scan.text
    memo = scan.statement_starts
    start = scan.logical_line_start(position)
    chain: list[int] = []
    spans = _multiline_quotes(scan) if scan.executable else []
    while start not in memo:
        if start == 0:
            memo[0] = 0
            break
        index = bisect.bisect_left(spans, (start, -1)) - 1
        if index >= 0 and spans[index][0] < start <= spans[index][1]:
            # The line begins inside a quoted word opened on an earlier line
            # (shell code: a quote may span lines): the statement starts there.
            chain.append(start)
            start = scan.logical_line_start(spans[index][0])
            continue
        previous_start = scan.logical_line_start(start - 1)
        previous = text[previous_start : start - 1]
        stripped = previous.rstrip()
        if stripped.endswith(("|", "&&", "||", "(")) or previous.count("(") > previous.count(")"):
            chain.append(start)
            start = previous_start
            continue
        memo[start] = start
        break
    result = memo[start]
    for line in chain:
        memo[line] = result
    return result


def _fetcher_positions(scan: _FileScan) -> list[int]:
    """Fetch commands: literal curl / wget / iwr ..., plus every expansion
    of a variable this file assigns a fetcher to anywhere
    (``F=curl; $F URL | sh``; over-approximated, no ordering)."""
    positions = scan.positions(_FETCHER)
    text = scan.text
    names: set[str] = set()
    for match in scan.finditer(_FETCHER_ASSIGNMENT):
        start = match.start()
        while start > 0 and text[start - 1] in _IDENTIFIER_CHARS:
            start -= 1
        name = text[start : match.start()]
        if name and not name[0].isdigit():
            names.add(name)
    if not names:
        return positions
    extra: list[int] = []
    length = len(text)
    for match in scan.finditer(_EXPANSION):
        begin = match.end() - 1
        end = begin
        while end < length and text[end] in _IDENTIFIER_CHARS:
            end += 1
        braced = text[match.start() + 1] == "{"
        if braced and text[end : end + 1] != "}":
            continue
        if text[begin:end] in names:
            extra.append(match.start())
            scan.charge(1, match.start())
    return sorted(set(positions) | set(extra))


_SHELL_TOKEN_WORD = re.compile(r"[^\s;|&()<>`'\"]{1,256}")


@dataclass
class _Tree:
    """What a nested command list holds, summarized for the stage it forms (a
    group, subshell or substitution body): the first decode stage (else
    fetch) and its command, the first runner, whether any command names a
    target and whether any is unknown; and, over what it RUNS (quoted text
    excluded), whether any step downloads, runs a file, or writes a value
    held in a variable (see carries_value)."""

    kind: str | None = None
    stage: Command | None = None
    runner: tuple[str, list[str]] | None = None
    target: bool = False
    unknown: bool = False
    download: bool = False
    run: bool = False
    carries: bool = False

    def add_kind(self, kind: str | None, command: Command | None) -> None:
        if kind == "decode" and self.kind != "decode" or kind == "fetch" and self.kind is None:
            self.kind, self.stage = kind, command

    def merge(self, other: _Tree, executed: bool = True) -> None:
        self.add_kind(other.kind, other.stage)
        if self.runner is None:
            self.runner = other.runner
        self.target = self.target or other.target
        self.unknown = self.unknown or other.unknown
        if executed:
            self.download = self.download or other.download
            self.run = self.run or other.run
            self.carries = self.carries or other.carries
# Command separators for the descriptor pre-check: `&&` and a background
# ` & ` (never the `&` of a `<&N` / `>&N` / `&>` redirection).
_FD_SEGMENT_BREAK = re.compile(r"[|;\n(`]|&&| & ")


_SEQUENCE_OP = re.compile(r";|&&|\|\|")
# Anything that can connect two commands or feed a process substitution.
_OPERATOR = re.compile(r"[|;&]|>\(")
# Empty parentheses after a command name: a function definition site.
_DEFINITION_PARENS = re.compile(r"[ \t]{0,4}\([ \t]{0,4}\)")
_EVAL_WORD = re.compile(r"\b(?:eval|iex|invoke-expression)\b", _I)
_PREFILTERS[_EVAL_WORD] = (("eval", "iex", "invoke-expression"),)
# A compound command's closing word standing alone (see shell_lex.CLOSER_WORDS).
_CLOSER_WORD = re.compile(r"(?:^|[\s;&])(\}|fi|done|esac)(?:[\s;&|)<>]|$)", re.MULTILINE)
_PREFILTERS[_CLOSER_WORD] = (("}", "fi", "done", "esac"),)
# Code an interpreter runs from an inline-code option (``python3 -c CODE``,
# ``perl -e CODE``, ``node --eval CODE``), and a call that executes a string.
_INLINE_CODE_LONG = frozenset({"--eval", "--print", "--command", "-command", "-encodedcommand"})
_EXEC_CALL = re.compile(r"\b(?:exec|eval|system|spawn|execsync|popen|run)\s{0,8}\(", _I)
# A command name that is one whole parameter expansion (``$p``, ``${p}``,
# ``${p:-x}``, an array element or all of one: ``${a[0]}``, ``"${a[@]}"``);
# group 1 or 2 is the name.
_WHOLE_EXPANSION = re.compile(
    r"\$([A-Za-z_][A-Za-z0-9_]{0,255})"
    r"|\$\{([A-Za-z_][A-Za-z0-9_]{0,255})(?:\[[^\]\n]{0,64}\])?(?:[:#%/^,@-][^}]{0,256})?\}"
)
# Where a member gives a variable a value it does not spell out: ``NAME=``
# (the value is checked), ``read`` / ``mapfile`` / ``readarray`` (their name
# arguments), ``for NAME in``, ``printf -v NAME``.
_ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]{0,255})\+?=")
_PREFILTERS[_ASSIGNMENT] = (("=",),)
_READ_WORD = re.compile(r"\b(?:read|mapfile|readarray)\s", _I)
_PREFILTERS[_READ_WORD] = (("read", "mapfile"),)
_FOR_NAME = re.compile(r"\b(?:for|select)\s{1,8}([A-Za-z_][A-Za-z0-9_]{0,255})\b")
_PREFILTERS[_FOR_NAME] = (("for", "select"),)
_PRINTF_V = re.compile(r"\bprintf\s{1,8}-v\s{0,8}([A-Za-z_][A-Za-z0-9_]{0,255})\b")
_PREFILTERS[_PRINTF_V] = (("printf",),)
_ASSIGNMENT_LEAD = frozenset(" \t\r\n;&|({`\"'")
_VALUE_STOP = frozenset(" \t\r\n;&|<>()")


def _strip_expansions(code: str) -> str:
    """``code`` without its parameter expansions, ``$( ... )`` (nested),
    backtick spans and substitution marks. One linear pass."""
    out: list[str] = []
    index = 0
    length = len(code)
    while index < length:
        char = code[index]
        if char == SUB_MARK:
            index += 1
            continue
        if char == "`":
            close = code.find("`", index + 1)
            index = length if close == -1 else close + 1
            continue
        if char == "$" and index + 1 < length:
            following = code[index + 1]
            if following == "(":
                depth = 0
                while index < length:
                    if code[index] == "(":
                        depth += 1
                    elif code[index] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    index += 1
                index += 1
                continue
            if following == "{":
                close = code.find("}", index)
                index = length if close == -1 else close + 1
                continue
            if following in "0123456789@*#?$!-":
                index += 2
                continue
            if following.isascii() and (following.isalpha() or following == "_"):
                index += 1
                while index < length and code[index] in _IDENTIFIER_CHARS:
                    index += 1
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _nonliteral_names(scan: _FileScan) -> frozenset[str]:
    """Variables the member gives a value it does not spell out: assigned an
    expansion or a substitution anywhere (``p=$(curl URL)``, ``x="$y"``), or
    named by ``read`` / ``mapfile`` / ``for`` / ``printf -v``. Over-
    approximated like the other shell state (no ordering or scope). One
    linear pass: an assignment inside an earlier value is skipped, and every
    name is charged."""
    if scan.nonliteral_names is not None:
        return scan.nonliteral_names
    text = scan.text
    length = len(text)
    names: set[str] = set()

    def add(name: str, position: int) -> None:
        scan.charge(1, position)
        names.add(name)

    covered = -1
    for match in scan.finditer(_ASSIGNMENT):
        start = match.start()
        if start < covered or (start > 0 and text[start - 1] not in _ASSIGNMENT_LEAD):
            continue
        index = match.end()
        literal = True
        while index < length and text[index] not in _VALUE_STOP:
            char = text[index]
            if char in "\"'":
                close = text.find(char, index + 1)
                close = length if close == -1 else close
                if char == '"' and ("$" in text[index:close] or "`" in text[index:close]):
                    literal = False
                index = close + 1
                continue
            if char in "$`":
                literal = False
            index += 1
        if index < length and text[index] == "(" and index == match.end():
            literal = False  # NAME=( ... ) array: its words are not read here
        covered = index
        if not literal:
            add(match.group(1), start)
    covered = -1
    for match in scan.finditer(_READ_WORD):
        if match.start() < covered:
            continue
        words, covered = _command_words(text, match.end())
        for word in words:
            if _is_shell_name(word):
                add(word, match.start())
    for pattern in (_FOR_NAME, _PRINTF_V):
        for match in scan.finditer(pattern):
            add(match.group(1), match.start())
    scan.nonliteral_names = frozenset(names)
    return scan.nonliteral_names


def _unknown_head_positions(scan: _FileScan, names: frozenset[str], signal: bool) -> list[int]:
    """Offsets of every shell word that may be an UNKNOWN command head (a
    defined name, any case; an expansion or parameter; a glob - see
    _unknown_token), anywhere: whether it is really a command head is
    decided structurally on the lexed statement (wrappers, options, values,
    assignments followed by _resolve_at). Without a fetch / decode in the
    member only a line holding an operator (``|``, ``;``, ``&``, ``>(``) can
    connect an unknown stage to a runner. In a member that fetches / decodes,
    every ``eval`` / ``iex`` too. One linear pass; every position is
    charged."""
    if not _indirection_applies(scan):
        return []
    text = scan.text
    shell_file = _shell_file(scan)
    operators = scan.positions(_OPERATOR) if not signal else []
    found: list[int] = []
    if signal or operators:
        for start, end in _shell_words(text):
            token = text[start:end]
            context = _context_at(scan, start, shell_file) if not shell_file else "shell"
            # A fail-closed wrapper (``env``) may hide the head it guards
            # behind an option it cannot parse. Every one is a candidate: the
            # position only gates analysis, and whether the head really is
            # unknown is decided once on the lexed command (_is_unknown_head).
            # Resolving the wrapper chain here, per token, re-read the rest of
            # the command for every ``env`` in it (quadratic in a run of them).
            fail_closed = token.casefold() in _FAIL_CLOSED_WRAPPERS
            if not fail_closed and token.casefold() not in names and not _unknown_token(token, context):
                continue
            if _DEFINITION_PARENS.match(text, end):
                continue  # `NAME()` defines a function; it runs nothing here
            if not signal and not _any_within(
                operators, scan.logical_line_start(start), scan.line_end(start)
            ):
                continue
            scan.charge(1, start)
            found.append(start)
    if signal:
        for match in scan.finditer(_EVAL_WORD):
            scan.charge(1, match.start())
            found.append(match.start())
        # A compound's closing word with an operator after it on its line
        # (``done | sh`` ending a multi-line loop): the output of a compound
        # the statement does not show (an opaque stage, see shell_lex).
        operators = scan.positions(_OPERATOR)
        for match in scan.finditer(_CLOSER_WORD):
            start = match.start(1)
            if _any_within(operators, match.end(), scan.line_end(start) + 1):
                scan.charge(1, start)
                found.append(start)
    return sorted(found)


def _scan_fetch_and_encoded(
    scan: _FileScan, sinks: list[int], budget: _LexBudget, depth: int = 0
) -> None:
    text = scan.text
    decoders = sorted(set(scan.positions(_DECODER)) | set(scan.pairs(_OPENSSL_CODEC, _DECODE_FLAG, same_command=True)))
    fetchers = _fetcher_positions(scan)
    # Shell indirection, over-approximated (see _indirection_names): defined
    # alias / function names are UNKNOWN commands - and runners.
    names = _indirection_names(scan)
    aliases = names
    shell_file = _shell_file(scan)
    signal = bool(fetchers or decoders)  # a real fetch / decode in the file
    unknown_positions = _unknown_head_positions(scan, names, signal)
    base = _severity(scan.executable, exec_sev="critical", prose_sev="high")

    lowered = {"critical": "high", "high": "medium"}

    def escalated(start: int, end: int, command: str | None = None) -> str:
        # A remote script served from a raw IP, paste site or tunnel is never
        # an install step a legitimate publisher needs.
        if _any_within(sinks, start, end):
            return "critical"
        # One level lower only when `command` - the fetch command alone - is
        # strictly "<fetcher> [plain flags] <one official installer URL>".
        if command is not None and sole_installer_fetch(command):
            return lowered[base]
        return base

    # ---- structural evaluation (shell lexer; see skills/shell_lex.py) -------
    fetcher_set = set(fetchers)
    decoder_set = set(decoders)
    fetcher_positions = sorted(fetcher_set)
    decoder_positions = sorted(decoder_set)
    candidate_set = fetcher_set | decoder_set | set(unknown_positions)

    def values(command: Command) -> list[str]:
        return [word.value for word in command.words]

    def head_index(words: list[str]) -> int | None:
        resolved = resolve_command(words)
        if resolved is None:
            return None
        index = len(words) - len(resolved[1]) - 1
        return index if 0 <= index < len(words) else None

    # Caches keyed by object id hold the object itself and check identity: a
    # command tree that was parsed, used and dropped (file-descriptor reads,
    # here-documents) frees its ids for reuse, so a bare id could return a
    # stale answer for an unrelated command.
    view_cache: dict[int, tuple[Command, Command]] = {}

    def view(command: Command) -> Command:
        """The command as the agent would run it. In prose (markdown, text)
        a command sits inside a sentence or behind a list / heading / prompt
        marker (``- curl URL | sh``, ``Install with curl URL | sh``,
        ``$ curl ...``): the command starts at the first of its leading words
        that is a fetcher, decoder, runner or executable path. Scripts are
        read as the shell reads them. No word cap: each word is examined
        once."""
        if scan.executable or len(command.words) < 2:
            return command
        key = id(command)
        entry = view_cache.get(key)
        if entry is not None and entry[0] is command:
            return entry[1]
        result = command
        words = values(command)
        k = 0
        while k < len(words):
            candidate = command.words[k]
            fetch_word = _any_within(fetcher_positions, candidate.start, candidate.end)
            if words[k] in _PROMPT_MARKERS or SUB_MARK in words[k]:
                k += 1
                continue  # `$ curl ...`; inline `code` is not a command name
            if k and not fetch_word and (
                words[k] == "." or words[k].startswith("-") or _NON_PLAIN.search(words[k])
            ):
                # `jq .`, `grep -r x .`, `'.items[]'`: an argument. Past the
                # first word only a plain command name starts a command.
                k += 1
                continue
            index, special = _resolve_at(words, k)
            interesting = False
            if index >= 0:
                word = command.words[index]
                name = words[index].rsplit("/", 1)[-1].casefold()
                interesting = (
                    _any_within(fetcher_positions, word.start, word.end)
                    or _any_within(decoder_positions, word.start, word.end)
                    or _any_within(unknown_positions, word.start, word.end)
                    or word.value.startswith("./")
                    or name == "chmod"
                    or name in _SOURCE_WORDS
                    or words[index].casefold() in names
                    or (
                        _is_interpreter_name(name)
                        and not is_data_only_invocation(name, words[index + 1 : index + 3])
                    )
                )
            elif special is not None:
                interesting = _runner([special[0], *special[1][:2]], aliases) is not None
            if not interesting:
                # Words this resolution consumed (wrapper options, the head)
                # cannot start a different command: skip them. Linear.
                k = max(k, index) + 1 if index >= 0 else len(words)
                continue
            if k:
                result = Command(
                    words=command.words[k:], redirects=command.redirects,
                    start=command.words[k].start, end=command.end, subshell=command.subshell,
                )
            break
        view_cache[key] = (command, result)
        view_cache[id(result)] = (result, result)
        return result

    kind_cache: dict[int, tuple[Command, str | None]] = {}
    # CommandList -> its summary (see tree_of), identity-checked.
    tree_cache: dict[int, tuple[CommandList, _Tree]] = {}

    def child_lists(command: Command) -> Iterator[tuple[CommandList, bool]]:
        """The lists nested in a command, as walk_lists visits them, with
        ``executed``: its subshell / compound body and substitution bodies
        (True), quoted strings (False)."""
        if command.subshell is not None:
            yield command.subshell, True
        words = list(command.words)
        words.extend(r.target for r in command.redirects if r.target is not None)
        for word in words:
            for nested in word.nested:
                yield nested, False
            for sub in word.subs:
                if sub.body is not None:
                    yield sub.body, True

    def tree_of(command_list: CommandList) -> _Tree:
        """Summary of a nested list and everything nested in it (see _Tree),
        conservatively: a fetch / runner / unknown command anywhere in it
        counts. Each list is summarized once per statement, so nested groups
        and substitutions stay linear."""
        key = id(command_list)
        entry = tree_cache.get(key)
        if entry is not None and entry[0] is command_list:
            return entry[1]
        tree = _Tree()
        tree_cache[key] = (command_list, tree)  # a list never contains itself
        for pipeline in command_list.pipelines:
            tree.download = tree.download or download_stage(pipeline) is not None
            first = pipeline.commands[0] if pipeline.commands else None
            if first is not None and first.words and not tree.run:
                tree.run = run_shape(first) is not None
            for command in pipeline.commands:
                if command.words:
                    tree.add_kind(stage_kind(command), command)
                    if tree.runner is None:
                        tree.runner = runner_of(command)
                    tree.target = tree.target or has_target(command)
                    tree.unknown = tree.unknown or is_unknown(command)
                    tree.carries = tree.carries or carries_value(command)
                elif command.opaque:
                    tree.unknown = tree.unknown or signal
                for child, executed in child_lists(command):
                    tree.merge(tree_of(child), executed)
        return tree

    def subshell_kind(command: Command) -> str | None:
        """The stage kind of a subshell / compound command, summarizing the
        commands it groups: decode wins, else fetch (conservative: a fetch
        anywhere in the group is a fetch stage)."""
        return tree_of(command.subshell).kind if command.subshell is not None else None

    def stage_kind(command: Command) -> str | None:
        """"decode" / "fetch" when the command (after wrappers) is a decoder
        or a fetch command, else None (cached per command). A subshell command
        (``(curl URL)``) summarizes its grouped commands."""
        if not command.words:
            return subshell_kind(command)
        if not (
            _any_within(fetcher_positions, command.words[0].start, command.end)
            or _any_within(decoder_positions, command.words[0].start, command.end)
        ):
            return None  # cheap reject before the prose view
        command = view(command)
        key = id(command)
        entry = kind_cache.get(key)
        if entry is None or entry[0] is not command:
            entry = (command, _stage_kind(command))
            kind_cache[key] = entry
        return entry[1]

    def runner_of(command: Command) -> tuple[str, list[str]] | None:
        """The resolved runner of a command, or of its subshell (the first
        runner found inside, conservatively), else None."""
        command = view(command)
        words = values(command)
        if words:
            return _runner(words, aliases)
        if command.subshell is not None:
            return tree_of(command.subshell).runner
        return None

    def is_runner(command: Command) -> bool:
        return runner_of(command) is not None

    def evaluate_indirect_eval(command: Command) -> None:
        """``eval "$x"`` / ``iex $x`` in a member that fetches or decodes:
        what runs cannot be known statically (over-approximated)."""
        if not signal or not command.words:
            return
        command = view(command)
        words = values(command)
        resolved = resolve_command(words)
        if resolved is not None and resolved[0] in _STRING_EVALUATORS and _is_unknown_head(
            words, names, context_of(command)
        ):
            scan.add("fetch_and_run.indirect", "critical", command.start)

    def context_of(command: Command) -> str:
        return "shell" if shell_file else _context_at(scan, command.start, False)

    def subshell_unknown(command: Command) -> bool:
        return command.subshell is not None and tree_of(command.subshell).unknown

    def is_unknown(command: Command) -> bool:
        command = view(command)
        if not command.words:
            if command.opaque:
                # The output of a compound opened outside this statement
                # (``done | sh``): unknown in a member that fetches / decodes.
                return signal
            return subshell_unknown(command)
        return _is_unknown_head(values(command), names, context_of(command))

    def evaluate_unknown_arguments(command: Command) -> None:
        """An UNKNOWN command given a runner as an argument, in a member that
        fetches / decodes: ``p sh`` after ``alias p='curl URL |'``, or a
        function piping into ``"$@"``. The alias may end in an operator and
        the function may run its arguments, so the argument may be the
        pipeline's runner (over-approximated, D3 review)."""
        if not signal or len(command.words) < 2:
            return
        command = view(command)
        words = values(command)
        index = _resolve_at(words, 0)[0]
        if index < 0 or not _is_unknown_head(words, names, context_of(command)):
            return
        if any(_runner_argument(word, names) for word in words[index + 1 :]) or any(
            body_kind(sub.body)[0] for word in command.words[1:] for sub in word.subs
        ):
            # A runner argument, or a fetched / decoded argument the unknown
            # command may run (``run "$(curl URL)"`` with ``run() { "$@"; }``).
            scan.add("fetch_and_run.indirect", "critical", command.start)

    def _stage_kind(command: Command) -> str | None:
        if not command.words:
            return None
        first = command.words[0]
        # Cheap reject: no fetch / decode position anywhere in the command.
        if not (
            _any_within(fetcher_positions, first.start, command.end)
            or _any_within(decoder_positions, first.start, command.end)
        ):
            return None
        index = head_index(values(command))
        if index is not None:
            word = command.words[index]
            if _any_within(decoder_positions, word.start, word.end):
                return "decode"
            if _any_within(fetcher_positions, word.start, word.end):
                return "fetch"
        return output_kind(command)

    def output_kind(command: Command) -> str | None:
        """A command that prints what a substitution or input process
        substitution in its words fetched or decoded (``echo "$(curl URL)"``,
        ``cat <(base64 -d f)``, ``cat < <(curl URL)``) is that kind of stage
        itself: what it writes is the download. In prose a lone backtick span
        is inline code, not a substitution."""
        found: str | None = None
        words = list(command.words)
        words.extend(r.target for r in command.redirects if r.target is not None and "<" in r.op)
        for word in words:
            for sub in word.subs:
                if sub.body is None or sub.kind == ">(" or sub.kind == "`" and not scan.executable:
                    continue
                kind = tree_of(sub.body).kind
                if kind == "decode":
                    return kind
                found = found or kind
        return found

    def subshell_has_target(command: Command) -> bool:
        return command.subshell is not None and tree_of(command.subshell).target

    def has_target(command: Command) -> bool:
        """A fetch names a target; a bare tool name is a markdown table cell
        (``| curl | bash |``). A first word longer than the fetcher name
        (``curl${IFS}URL``) carries one. A subshell fetch (``(curl URL)``)
        carries the target of the fetch it groups."""
        command = view(command)
        words = values(command)
        if not words:
            return subshell_has_target(command)
        index = head_index(words)
        if index is None:
            return False
        word = command.words[index]
        match = _FETCHER.match(text, word.start)
        if match is not None and match.end() < word.end:
            return True
        if any(not item.startswith("-") for item in words[index + 1 :]):
            return True
        # A stage printing a substitution's fetch (``cat < <(curl URL)``)
        # carries the target of the fetch inside it.
        return any(
            sub.body is not None and tree_of(sub.body).target
            for redirect in command.redirects if redirect.target is not None
            for sub in redirect.target.subs
        )

    def body_kind(body: CommandList | None) -> tuple[str | None, Command | None]:
        """("decode" | "fetch", the stage) found in a substitution body."""
        if body is None:
            return None, None
        tree = tree_of(body)
        return tree.kind, tree.stage

    def command_text(command: Command) -> str | None:
        if command.end - command.start > _PIPE_WINDOW:
            return None
        return text[command.start : command.end]

    def report_sub(sub: Sub) -> None:
        kind, stage = body_kind(sub.body)
        if kind is None or stage is None:
            return
        if kind == "decode":
            scan.add("encoded_exec.decode_pipe", "critical", sub.start)
            return
        rule = "fetch_and_run.process_substitution" if sub.kind == "<(" else "fetch_and_run.command_substitution"
        single = sub.body is not None and len(sub.body.pipelines) == 1 and len(sub.body.pipelines[0].commands) == 1
        scan.add(rule, escalated(sub.start, sub.end, command=command_text(stage) if single else None), sub.start)

    def runs_output_procsub(command: Command) -> bool:
        """``tee >(sh)`` / ``> >(sh)``: an output process substitution whose
        command runs what it reads."""
        words = list(command.words) + [r.target for r in command.redirects if r.target is not None]
        for word in words:
            for sub in word.subs:
                if sub.kind == ">(" and sub.body is not None:
                    for pipeline in sub.body.pipelines:
                        if pipeline.commands and is_runner(pipeline.commands[0]):
                            return True
        return False

    def evaluate_unknown_pipeline(commands: list[Command]) -> None:
        """An UNKNOWN stage (see _is_unknown_head) feeding a sink that runs
        what it reads: a later stage (``f | sh``, ``$F URL | sh``; ``f | g``
        in a member that fetches / decodes) or an output process
        substitution of the stage itself or a later one (``f URL > >(sh)``,
        ``f URL | tee >(sh)``). Every pipeline, one command or more.
        Over-approximated on purpose (D3 review)."""
        unknown = [is_unknown(command) for command in commands]
        carries = [not unknown[i] and carries_value(command) for i, command in enumerate(commands)]
        if not any(unknown) and not any(carries):
            return
        literal_runner_after = False
        unknown_after = False
        for i in range(len(commands) - 1, -1, -1):
            if runs_output_procsub(view(commands[i])):
                literal_runner_after = True  # it runs what this stage writes
            if (unknown[i] and (literal_runner_after or (unknown_after and signal))) or (
                carries[i] and literal_runner_after
            ):
                scan.add("fetch_and_run.indirect", "critical", view(commands[i]).start)
                return
            if unknown[i]:
                unknown_after = True
            else:
                found = runner_of(commands[i])
                if found is not None and not _unknown_token(found[0]) and "$" not in found[0]:
                    literal_runner_after = True

    def carries_value(command: Command) -> bool:
        """In shell code of a member that fetches / decodes: a stage that
        writes a value held in a variable or another command's output
        (``echo "$x"``, ``printf %s "$(cat f)"``, ``cat <<< "$x"``) - it may
        be what was fetched (over-approximated, D3). A fetch / decode stage is
        the pipeline rule's finding instead."""
        if not signal or not command.words or context_of(command) != "shell":
            return False
        command = view(command)
        words = values(command)
        index = head_index(words)
        if index is None or stage_kind(command) is not None:
            return False
        if any(_non_literal(word) for word in words[index + 1 :]):
            return True
        return any(
            r.target is not None and r.op.lstrip("0123456789") in ("<", "<<<") and _non_literal(r.target.value)
            for r in command.redirects
        )

    def code_is_unknown(name: str, code: str) -> bool:
        """The code a runner is given (a command string, a here-string, an
        inline-code option) may be content held in a variable: for a shell
        (or a name that cannot be known), its command head is unknown
        (``sh -c "$p"``, ``bash <<< 'eval "$x"'``); for another interpreter
        the code is nothing but expansions (``python3 -c "$p"``) or passes
        one to a call that executes a string (``node -e "eval('$p')"``)."""
        if not _non_literal(code):
            return False
        if name in _SHELL_NAMES or name in _SOURCE_WORDS or _unknowable_head(name) or name in names:
            words = _command_words(code.replace(SUB_MARK, "$_"), 0)[0]
            return bool(words) and _is_unknown_head(words, names)
        rest = _strip_expansions(code)
        return not rest.strip(" \t\r\n'\";") or _EXEC_CALL.search(code) is not None

    def inline_code(rest: list[str]) -> list[str]:
        """Every code an interpreter (not a shell) may run from an inline-code
        option: ``-c CODE`` / ``-e CODE`` / ``-E`` / ``-p`` / ``-r`` (a short
        cluster ending in one, or the code attached), ``--eval`` /
        ``--print`` / ``--command`` / ``-Command`` (``=CODE`` attached).
        Options taking a value (``-W ignore``, ``-I lib``, ``node -r mod``)
        are not modelled, so EVERY word is examined, fail closed: a value or
        a script argument that looks like a code option is also read."""
        found: list[str] = []
        for position, word in enumerate(rest):
            if not word.startswith("-") or word == "-":
                continue
            following = rest[position + 1] if position + 1 < len(rest) else None
            key, equals, attached = word.partition("=")
            if key.casefold() in _INLINE_CODE_LONG:
                if equals:
                    found.append(attached)
                elif following is not None:
                    found.append(following)
                continue
            letters = word[1:]
            if word.startswith("--") or not letters.isalpha() and not any(c in letters for c in "$\x01"):
                continue
            for index, letter in enumerate(letters):
                if letter in "ceEpr":
                    tail = letters[index + 1 :]
                    if tail:
                        found.append(tail)
                    elif following is not None:
                        found.append(following)
                    break
        return found

    def evaluate_indirect_value(command: Command) -> None:
        """In shell code of a member that fetches / decodes, content held in a
        variable may be what was fetched; running it is over-approximated as
        indirect (the rule ``eval "$x"`` already follows): a shell or
        interpreter given non-literal code (``sh -c "$p"``, ``python3 -c
        "$p"``, ``bash <<< "$x"``), a runner reading a descriptor named by an
        expansion (``sh <&"${COPROC[0]}"``), or a command whose NAME is an
        expansion (``$p``, ``exec $p``, ``"$x"``) even when nothing reads its
        output."""
        if not signal or not command.words or context_of(command) != "shell":
            return
        command = view(command)
        words = values(command)
        if stage_kind(command) is not None and any(
            r.target is not None and r.op.lstrip("0123456789") == ">&" and _non_literal(r.target.value)
            for r in command.redirects
        ):
            # A fetch / decode written to a descriptor named by an expansion
            # (``curl URL >&"${COPROC[1]}"``): a coprocess may run it.
            scan.add("fetch_and_run.indirect", "critical", command.start)
            return
        index = _resolve_at(words, 0)[0]
        if index >= 0:
            whole = _WHOLE_EXPANSION.fullmatch(words[index])
            if whole is not None and (whole.group(1) or whole.group(2)) in _nonliteral_names(scan):
                # ``p=$(curl URL); $p``: the variable's value is the command.
                scan.add("fetch_and_run.indirect", "critical", command.start)
                return
        runner = _runner(words, aliases)
        if runner is None:
            return
        name, rest = runner
        codes: list[str] = []
        found = _command_string_at(words, names)
        if found is not None:
            codes.append(found[1])
        elif name not in _SHELL_NAMES:
            codes.extend(inline_code(rest))
        # Substitutions whose output the runner runs: an input process
        # substitution, a here-string or input redirection, or any argument
        # of a command string / source word (``sh <(f)``, ``sh -c "$(f)"``).
        executes = found is not None or name in _SOURCE_WORDS
        subs = [
            sub for word in command.words[1:] for sub in word.subs
            if sub.body is not None and (sub.kind == "<(" or executes and sub.kind != ">(")
        ]
        for redirect in command.redirects:
            if redirect.target is None:
                continue
            op = redirect.op.lstrip("0123456789")
            if op == "<<<":
                codes.append(redirect.target.value)
            elif op == "<&" and _non_literal(redirect.target.value):
                scan.add("fetch_and_run.indirect", "critical", command.start)
                return
            if op in ("<", "<<<"):
                subs.extend(sub for sub in redirect.target.subs if sub.body is not None and sub.kind != ">(")
        for sub in subs:
            assert sub.body is not None
            tree = tree_of(sub.body)
            if tree.kind is None and (tree.unknown or tree.carries):
                # What runs is a value or an unknown command's output (a
                # function wrapping a fetch): over-approximated (D3).
                scan.add("fetch_and_run.indirect", "critical", command.start)
                return
        if any(code_is_unknown(name, code) for code in codes):
            scan.add("fetch_and_run.indirect", "critical", command.start)

    def evaluate_pipeline(pipeline: Pipeline) -> None:
        """An earlier stage fetches / decodes and a later stage runs (or a
        stage feeds an output process substitution that runs). Linear: each
        stage is classified once and the nearest later runner is precomputed."""
        commands = pipeline.commands
        count = len(commands)
        if count == 0:
            return
        kinds = [stage_kind(command) for command in commands]
        if not any(kinds):
            return
        runners = [is_runner(command) for command in commands]
        procsubs = [runs_output_procsub(command) for command in commands]
        next_runner = [count] * (count + 1)
        next_procsub = [count] * (count + 1)
        for index in range(count - 1, -1, -1):
            next_runner[index] = index if runners[index] else next_runner[index + 1]
            next_procsub[index] = index if procsubs[index] else next_procsub[index + 1]
        for i, command in enumerate(commands):
            kind = kinds[i]
            if kind is None or (kind == "fetch" and not has_target(command)):
                continue
            runner_at = next_runner[i + 1] if i + 1 <= count else count
            j = min(runner_at, next_procsub[i])
            if j >= count:
                continue
            if kind == "decode":
                scan.add("encoded_exec.decode_pipe", "critical", view(command).start)
            else:
                downgrade = command_text(command) if j == i + 1 and runners[j] else None
                severity = escalated(command.start, commands[j].start, command=downgrade)
                scan.add("fetch_and_run.pipe_to_interpreter", severity, view(command).start)
            return

    analyzed_strings: set[int] = set()
    # (word start, word end, rule, severity) found inside command strings;
    # added after the statements are evaluated (see add_string_hits).
    string_hits: list[tuple[int, int, str, str]] = []

    def analyze_command_string(command: Command) -> None:
        """A command string a shell or wrapper runs (``bash -c STR``,
        ``su -c STR``, ``env -S STR`` / ``--split-string=STR``, escaped or
        quoted any way) is shell text of its own: scan its decoded value and
        report what runs at the word that carries it (the worst hit of each
        rule, once per word)."""
        command = view(command)
        found = _command_string_at(values(command), names)
        if found is None:
            return
        index, value = found
        word = command.words[index]
        if word.start in analyzed_strings:
            return
        analyzed_strings.add(word.start)
        value = value.replace(SUB_MARK, "")
        if len(value.strip()) < 4:
            return
        if depth >= _MAX_STRING_DEPTH or budget.remaining < len(value):
            # Nested deeper than analyzed, or past the analysis budget: the
            # member fails closed (never a silent skip).
            raise _AnalysisExhausted(word.start)
        budget.remaining -= len(value)
        inner = _scan_patterns(scan.path, value, budget, depth=depth + 1, shell=True)
        worst: dict[str, str] = {}
        for hit in inner.hits:
            if (
                RULES[hit.rule_id].family in ("fetch_and_run", "encoded_exec")
                or hit.rule_id == "hidden_content.analysis_budget"
            ) and SEVERITY_RANK[hit.severity] > SEVERITY_RANK[worst.get(hit.rule_id, "none")]:
                worst[hit.rule_id] = hit.severity
        for rule_id, severity in worst.items():
            # The span is the whole command: an `eval` string is its joined
            # arguments, a forwarded string any of the call's words.
            string_hits.append((word.start, max(word.end, command.end), rule_id, severity))

    def add_string_hits() -> None:
        """Report command-string findings, except where the same rule already
        fired at least as severely INSIDE the word: a quoted string is also
        evaluated as text (``bash -c 'curl URL | sh'`` finds the pipeline at
        ``curl``), and the same finding must not be reported twice."""
        if not string_hits:
            return
        existing: dict[str, list[tuple[int, int]]] = {}
        for hit in scan.hits:
            existing.setdefault(hit.rule_id, []).append((hit.position, SEVERITY_RANK[hit.severity]))
        for found in existing.values():
            found.sort()
        for start, end, rule_id, severity in string_hits:
            found = existing.get(rule_id, [])
            index = bisect.bisect_left(found, (start, -1))
            rank = SEVERITY_RANK[severity]
            covered = False
            while index < len(found) and found[index][0] < end:
                if found[index][1] >= rank:
                    covered = True
                    break
                index += 1
            if not covered:
                scan.add(rule_id, severity, start)

    def evaluate_group_input(command: Command) -> None:
        """A compound / subshell reading a fetch or decode on its input
        (``{ sh; } < <(curl URL)``, ``if true; then bash; fi <<< "$(curl
        URL)"``) when something inside it runs what it reads (or, in a member
        that fetches / decodes, is unknown)."""
        if command.subshell is None:
            return
        tree = tree_of(command.subshell)
        if tree.runner is None and not (signal and tree.unknown):
            return
        for redirect in command.redirects:
            if redirect.target is None or redirect.op.lstrip("0123456789") not in ("<", "<<<", "<>"):
                continue
            for sub in redirect.target.subs:
                if sub.kind != ">(" and body_kind(sub.body)[0] is not None:
                    report_sub(sub)
                    return

    def evaluate_command(command: Command) -> None:
        if not command.words:
            evaluate_group_input(command)
            return
        if not (
            command.words[0].value == "exec"
            or any(word.subs for word in command.words)
            or any(r.target is not None and r.target.subs for r in command.redirects)
        ):
            return  # nothing a runner could execute: no substitution, no fd
        command = view(command)
        words = values(command)
        if not command.words:
            return
        # A command NAME computed from a download (`$(curl -s URL)` alone).
        head = command.words[0]
        if head.subs and SUB_MARK in head.value and resolve_command(words) is not None:
            if head.value.strip() == SUB_MARK:
                for sub in head.subs:
                    # In prose a lone backtick span is inline code, not a
                    # substitution the agent runs.
                    if scan.executable or sub.kind != "`":
                        report_sub(sub)
        if words[0] == "exec":
            evaluate_exec_fd(command)
        runner = _runner(words, aliases)
        if runner is None:
            return
        name, rest = runner
        candidates: list[Sub] = []
        executes_arguments = name in _SOURCE_WORDS or "-c" in rest or SUB_MARK in name
        for word in command.words[1:]:
            for sub in word.subs:
                if executes_arguments or sub.kind == "<(":
                    candidates.append(sub)
        for redirect in command.redirects:
            if redirect.target is None:
                continue
            if redirect.op.endswith("<") or redirect.op.endswith("<<<"):
                candidates.extend(redirect.target.subs)
        for sub in candidates:
            report_sub(sub)

    def evaluate_exec_fd(command: Command) -> None:
        """``exec 3< <(curl URL)`` then ``bash <&3``."""
        for redirect in command.redirects:
            if not redirect.op.endswith("<") or redirect.target is None:
                continue
            descriptor = redirect.op[:-1] or "0"
            subs = [sub for sub in redirect.target.subs if sub.kind == "<("]
            if not subs or body_kind(subs[0].body)[0] is None:
                continue
            if runner_read_after(descriptor, command.end):
                report_sub(subs[0])
                return

    # Descriptor -> offsets of its `<&N` reads: ONE index for the file,
    # built once, every read charged against the item budget (so a member of
    # millions of reads stops early and fails closed), compact int64 arrays.
    fd_reads: dict[str, array[int]] | None = None
    # Descriptor -> per-read "a runner reads it" (0 unknown, 1 no, 2 yes)
    # and the last answer (query index, runner index) for forward queries.
    fd_flags: dict[str, bytearray] = {}
    fd_answers: dict[str, tuple[int, int]] = {}
    # The last read statement lexed (consecutive reads often share one).
    fd_last_statement: list[object] = [-1, set()]

    def fd_index() -> dict[str, array[int]]:
        nonlocal fd_reads
        if fd_reads is None:
            reads: dict[str, array[int]] = {}
            pending = 0
            total = 0
            for match in scan.finditer(_FD_READ):
                reads.setdefault(match.group(1), array("q")).append(match.start())
                pending += 1
                total += 1
                if total > MAX_FD_READS:
                    # No legitimate member reads descriptors this often.
                    raise _AnalysisExhausted(match.start())
                if pending == 4096:  # charged in blocks: stops early, cheaply
                    scan.charge(pending, match.start())
                    pending = 0
            scan.charge(pending, 0)
            fd_reads = reads
        return fd_reads

    def runner_descriptors(statement_at: int) -> set[str]:
        if fd_last_statement[0] == statement_at:
            return fd_last_statement[1]  # type: ignore[return-value]
        tree, end = lexer.statement(statement_at)
        # Lexing a read's statement is shell analysis like any other.
        budget.remaining -= end - statement_at
        if budget.remaining < 0:
            raise _AnalysisExhausted(statement_at)
        found: set[str] = set()
        for command_list, quoted in walk_lists(tree):
            if quoted:
                continue
            for pipeline in command_list.pipelines:
                for reader in pipeline.commands:
                    read = {
                        r.target.value for r in reader.redirects
                        if r.op.endswith("<&") and r.target is not None
                    }
                    if read and is_runner(reader):
                        found |= read
        fd_last_statement[0] = statement_at
        fd_last_statement[1] = found
        return found

    # Streaming pre-check of each read's command, ONE pass per descriptor:
    # the command is the text since the last separator before the read; a
    # read is lexed (and charged) only when a word of it could run something
    # (an interpreter, a wrapper, a source word, an unknowable or
    # state-defined name). Millions of `cat <&3` reads cost one regex pass.
    fd_plausible: dict[str, bytearray] = {}
    token_verdicts: dict[str, bool] = {}

    def plausible_token(token: str) -> bool:
        verdict = token_verdicts.get(token)
        if verdict is None:
            name = token.rsplit("/", 1)[-1].casefold()
            verdict = (
                _is_interpreter_name(name)
                or name in _SOURCE_WORDS
                or name in _WRAPPERS
                or _unknowable_head(token)
                or token.casefold() in names
            )
            if len(token_verdicts) >= 65536:
                token_verdicts.clear()
            token_verdicts[token] = verdict
        return verdict

    def plausible_reads(descriptor: str, reads: array[int]) -> bytearray:
        found = fd_plausible.get(descriptor)
        if found is not None:
            return found
        found = bytearray(len(reads))
        breaks = _FD_SEGMENT_BREAK.finditer(text)
        upcoming = next(breaks, None)
        segment_start = 0
        checked = 0
        plausible = False
        for index, position in enumerate(reads):
            while upcoming is not None and upcoming.start() < position:
                segment_start, plausible = upcoming.end(), False
                upcoming = next(breaks, None)
            if not plausible:
                for match in _SHELL_TOKEN_WORD.finditer(text, max(checked, segment_start), position):
                    if plausible_token(match.group(0)):
                        plausible = True
                        break
            checked = position
            found[index] = plausible
        fd_plausible[descriptor] = found
        return found

    def runner_read_after(descriptor: str, after: int) -> bool:
        """Is there a read of ``descriptor`` at or after ``after`` whose
        command runs what it reads? Reads are judged lazily, each at most
        once; forward queries reuse the previous answer, so this is linear."""
        reads = fd_index().get(descriptor)
        if not reads:
            return False
        count = len(reads)
        flags = fd_flags.setdefault(descriptor, bytearray(count))
        plausible = plausible_reads(descriptor, reads)
        index = bisect.bisect_left(reads, after)
        previous = fd_answers.get(descriptor)
        if previous is not None and previous[0] <= index <= previous[1]:
            return previous[1] < count
        start = index
        while index < count:
            if not flags[index]:
                runs = bool(plausible[index]) and descriptor in runner_descriptors(
                    scan.logical_line_start(reads[index])
                )
                flags[index] = 2 if runs else 1
            if flags[index] == 2:
                break
            index += 1
        fd_answers[descriptor] = (start, index)
        return index < count

    def download_stage(pipeline: Pipeline) -> tuple[Command, str, set[str]] | None:
        for command in pipeline.commands:
            command = view(command)
            kind = stage_kind(command)
            if kind is None:
                continue
            outputs = _download_outputs(text[command.start : command.end])
            redirected = [r for r in command.redirects if r.op.lstrip("0123456789") in (">", ">>", ">|") and r.target is not None]
            outputs |= {r.target.value for r in redirected if r.target is not None}
            outputs |= {value.rsplit("/", 1)[-1] for value in list(outputs)}
            if kind == "fetch":
                is_wget = text[command.start : command.start + 4].casefold() == "wget"
                if outputs or is_wget:
                    return command, kind, outputs
            elif redirected:
                return command, kind, outputs
        return None

    def nested_pipelines(command: Command) -> Iterator[Pipeline]:
        """Every pipeline a subshell / compound command runs (quoted text
        excluded), however deeply nested."""
        if command.subshell is not None:
            for command_list, quoted in walk_lists(command.subshell):
                if not quoted:
                    yield from command_list.pipelines

    def stage_downloads(pipeline: Pipeline) -> list[tuple[Command, str, set[str]]]:
        """The download of a step: its own (download_stage), or any inside a
        compound / subshell it runs (``if ...; then curl -o x URL; fi``)."""
        found = download_stage(pipeline)
        result = [found] if found is not None else []
        for command in pipeline.commands:
            if command.subshell is not None and tree_of(command.subshell).download:
                for inner in nested_pipelines(command):
                    inner_found = download_stage(inner)
                    if inner_found is not None:
                        result.append(inner_found)
        return result

    def run_candidates(pipeline: Pipeline) -> list[Command]:
        """Commands of a step that may run a download: its first command, and
        the first command of every pipeline inside a compound / subshell it
        runs (``{ sh x; }``, ``if [ -f x ]; then sh x; fi``)."""
        result = [pipeline.commands[0]] if pipeline.commands and pipeline.commands[0].words else []
        for command in pipeline.commands:
            if command.subshell is not None and tree_of(command.subshell).run:
                for inner in nested_pipelines(command):
                    if inner.commands and inner.commands[0].words:
                        result.append(inner.commands[0])
        return result

    def evaluate_unknown_downloads(pipelines: list[Pipeline]) -> None:
        """An UNKNOWN command (see _is_unknown_head) whose argument a later
        command of the same list runs (inside a compound too): ``f URL -o
        /tmp/x; sh /tmp/x`` - the unknown command may be the download
        (over-approximated). Linear: run targets are indexed once, each
        unknown step is looked up."""
        steps = [
            i for i, pipeline in enumerate(pipelines)
            if pipeline.commands
            and _any_within(unknown_positions, pipeline.commands[0].start, pipeline.commands[0].end)
            and is_unknown(pipeline.commands[0])
        ]
        if not steps:
            return
        runs: dict[str, list[int]] = {}
        for j, pipeline in enumerate(pipelines):
            for candidate in run_candidates(pipeline):
                info = run_shape(candidate)
                if info is None:
                    continue
                for target in info[1]:
                    if target == info[0] and "/" not in target:
                        continue  # the runner's own name (`sh`), not what it runs
                    runs.setdefault(target, []).append(j)
                    runs.setdefault(target.rsplit("/", 1)[-1], []).append(j)
        for i in steps:
            command = view(pipelines[i].commands[0])
            arguments = set()
            for word in values(command)[1:]:
                arguments.update(_argument_paths(word))
            for redirect in command.redirects:
                if redirect.target is not None:
                    arguments.add(redirect.target.value)
            arguments.discard("")
            arguments |= {word.rsplit("/", 1)[-1] for word in list(arguments)}
            for argument in arguments:
                indices = runs.get(argument)
                if indices and indices[-1] > i:
                    scan.add("fetch_and_run.indirect", "critical", command.start)
                    return

    def evaluate_downloads(command_list: CommandList) -> None:
        """A download, then a later command of the same list that runs it -
        any number of steps later, either one inside a compound / subshell
        (``curl -o x URL; if [ -f x ]; then sh x; fi``, ``wget -O x URL; {
        sh x; }``). Linear: each step's runs are indexed once and the next
        running step after each position is precomputed; a command named by
        an absolute path (``/tmp/x``) counts only when it is the download,
        found by exact path / basename lookup."""
        pipelines = command_list.pipelines
        count = len(pipelines)
        if count < 2:
            return  # nothing can follow the download in this list
        evaluate_unknown_downloads(pipelines)
        stages = [stage_downloads(pipeline) for pipeline in pipelines]
        if not any(stages):
            return
        generic = [False] * count
        absolute: dict[str, list[int]] = {}
        # Step -> (every target it runs, with basenames; their joined text
        # for paths inside a target: ``bash -c ". /tmp/x"``).
        targets_of: dict[int, tuple[set[str], str]] = {}
        for j, pipeline in enumerate(pipelines):
            exact: set[str] = set()
            for candidate in run_candidates(pipeline):
                info = run_shape(candidate)
                if info is None:
                    continue
                first, targets = info
                exact |= targets
                if "/" in first and not first.startswith("./"):
                    for target in targets:
                        absolute.setdefault(target, []).append(j)
                        absolute.setdefault(target.rsplit("/", 1)[-1], []).append(j)
                else:
                    generic[j] = True
            if exact:
                exact |= {target.rsplit("/", 1)[-1] for target in list(exact)}
                targets_of[j] = (exact, "\n".join(sorted(exact)))
        next_generic = [count] * (count + 1)
        for j in range(count - 1, -1, -1):
            next_generic[j] = j if generic[j] else next_generic[j + 1]
        searched: dict[tuple[int, str], bool] = {}

        def linked_to(j: int, outputs: set[str]) -> bool:
            exact, joined = targets_of[j]
            if outputs & exact:
                return True
            for out in outputs:
                if len(out) < 4 or "/" not in out:
                    continue
                key = (j, out)
                if key not in searched:
                    # Each (step, path) is searched once; the search is
                    # charged to the analysis budget (fails closed).
                    budget.remaining -= len(joined)
                    if budget.remaining < 0:
                        raise _AnalysisExhausted(pipelines[j].start)
                    searched[key] = out in joined
                if searched[key]:
                    return True
            return False

        for i, found in enumerate(stages):
            for stage, kind, outputs in found:
                j = next_generic[i + 1]
                for output in outputs:
                    indices = absolute.get(output)
                    if indices:
                        at = bisect.bisect_right(indices, i)
                        if at < len(indices) and indices[at] < j:
                            j = indices[at]
                if j >= count:
                    continue
                linked = bool(outputs) and linked_to(j, outputs)
                if not generic[j] and not linked:
                    continue  # `&& /usr/bin/make`: only a run of the download counts
                if kind == "decode":
                    scan.add("encoded_exec.decode_pipe", "critical", stage.start)
                    continue
                severity = _severity(scan.executable, exec_sev="high", prose_sev="medium")
                if linked:
                    severity = _severity(scan.executable, exec_sev="critical", prose_sev="high")
                if _any_within(sinks, stage.start, pipelines[j].start):
                    severity = "critical"
                scan.add("fetch_and_run.download_execute", severity, stage.start)

    def run_shape(command: Command) -> tuple[str, set[str]] | None:
        """(command word, targets) when ``command`` runs a file or stdin,
        else None."""
        command = view(command)
        words = values(command)
        resolved = resolve_command(words)
        if resolved is None:
            return None
        name, rest = resolved
        if is_data_only_invocation(name, rest):
            return None
        index = head_index(words)
        first = words[index] if index is not None else ""
        args = [arg for arg in rest if not arg.startswith("-")]
        stdin = [
            r.target.value for r in command.redirects
            if r.target is not None and r.op in ("<", "0<") and not r.target.subs
        ]
        runner = _runner(words, aliases) is not None
        runs = (
            (name == "chmod" and any(_CHMOD_EXEC.fullmatch(arg) for arg in rest))
            or first.startswith("./")
            or (runner and bool(args or stdin))
            or "/" in first
        )
        if not runs:
            return None
        return first, set(args) | set(stdin) | ({first} if first else set())

    lexer = ShellLexer(
        text, comments=scan.executable, interesting=sorted(candidate_set),
        prose=not scan.executable,
    )
    if candidate_set:
        covered = -1
        last_start = -1
        file_remaining = LEX_FILE_BUDGET
        for position in sorted(candidate_set):
            if position < covered:
                continue
            start = _statement_start(scan, position)
            if start == last_start:
                continue
            last_start = start
            if file_remaining <= 0 or budget.remaining <= 0:
                scan.add("hidden_content.analysis_budget", "critical", position)
                break
            stop = start + min(file_remaining, budget.remaining)
            tree, end = lexer.statement(start, stop)
            if lexer.too_deep is not None:
                # Substitutions / subshells nested past what the lexer parses.
                raise _AnalysisExhausted(lexer.too_deep)
            if end >= stop and stop < len(text) and text[end : end + 1] != "\n":
                # The statement runs past the analysis budget: fail closed.
                scan.add("hidden_content.analysis_budget", "critical", position)
            covered = max(end, position + 1)
            file_remaining -= end - start
            budget.remaining -= end - start
            # The per-command caches only serve one statement's tree: drop
            # them so the trees of earlier statements are freed.
            view_cache.clear()
            kind_cache.clear()
            tree_cache.clear()
            for command_list, quoted in walk_lists(tree):
                # Quoted text (``echo 'curl URL | sh' >> ~/.bashrc``) is
                # checked for fetch-and-run pipelines only; substitution,
                # download and file-descriptor rules apply to what runs -
                # including a COMMAND STRING a shell runs (scanned on its own).
                for pipeline in command_list.pipelines:
                    evaluate_pipeline(pipeline)
                    if not quoted and _any_within(unknown_positions, pipeline.start, pipeline.end):
                        # Unknown commands only where they run (never in quoted text).
                        evaluate_unknown_pipeline(pipeline.commands)
                    if not quoted:
                        for command in pipeline.commands:
                            evaluate_command(command)
                            evaluate_indirect_eval(command)
                            if _any_within(unknown_positions, command.start, command.end):
                                evaluate_indirect_value(command)
                            if _any_within(unknown_positions, command.start, command.end):
                                evaluate_unknown_arguments(command)
                            analyze_command_string(command)
                if not quoted:
                    evaluate_downloads(command_list)
        add_string_hits()

    _scan_heredocs(scan, base, aliases, fetcher_set, decoders)

    ps_download = scan.positions(_PS_DOWNLOAD)
    ps_b64 = scan.positions(_PS_FROM_B64)
    if ps_download or ps_b64:
        for match in scan.finditer(_PS_IEX):
            start = match.start()
            if text[max(0, start - 8) : start].rstrip().endswith("|"):
                continue  # `irm URL | iex` is the pipe-to-interpreter rule's finding
            floor = scan.logical_line_start(start)
            end = scan.line_end(start)
            if _any_within(ps_b64, floor, end):
                scan.add("encoded_exec.powershell_encoded", "critical", start)
            elif _any_within(ps_download, floor, end):
                scan.add("fetch_and_run.powershell", escalated(floor, end), start)

    for match in scan.finditer(_REMOTE_EXEC):
        scan.add("fetch_and_run.remote_exec", base, match.start())


    if scan.context in {"hook", "config"}:
        for match in scan.finditer(_NPX_LATEST):
            scan.add("fetch_and_run.unpinned_npx", "medium", match.start())

    for match in scan.finditer(_EVAL_DECODED):
        scan.add("encoded_exec.eval_decoded", "critical", match.start())
    for position in scan.pairs(_POWERSHELL, _ENCODED_COMMAND):
        scan.add("encoded_exec.powershell_encoded", "critical", position)


def _scan_heredocs(
    scan: _FileScan,
    base: str,
    aliases: frozenset[str],
    fetcher_set: set[int],
    decoders: list[int],
) -> None:
    """``bash <<EOF`` (unquoted delimiter) whose body expands a fetch:
    ``$(curl URL)`` / backtick curl. The body becomes the interpreter's
    script. No size cap: bodies are disjoint and each is searched once; the
    command before ``<<`` is parsed once per separator-delimited segment."""
    text = scan.text
    length = len(text)
    body_end = -1
    segment_start = -1  # start of the command the previous `<<` belonged to
    segment_runner = False
    scanned = 0  # separators are searched incrementally (linear)
    last_separator = -1
    for match in scan.finditer(_HEREDOC):
        quoted = bool(match.group(1))
        start = match.start()
        if start < body_end:
            continue  # inside the previous here-document's body: stays linear
        if start > 0 and text[start - 1] == "<":
            continue  # `<<<` here-string, handled with command substitutions
        line_start = scan.logical_line_start(start)
        scanned = max(scanned, line_start)
        if last_separator < line_start - 1:
            last_separator = line_start - 1
        while scanned < start:
            if text[scanned] in "|;&\n(`":
                last_separator = scanned
            scanned += 1
        command_start = last_separator + 1
        if command_start != segment_start:
            segment_start = command_start
            before = _command_words(text, command_start, start - command_start)[0]
            segment_runner = _runner(before, aliases) is not None
        if not segment_runner:
            continue
        line_end = scan.line_end(match.end())
        if line_end >= length:
            continue
        body_start = line_end
        name_end = match.end()
        while name_end < length and text[name_end] in _IDENTIFIER_CHARS:
            name_end += 1  # a delimiter longer than the pattern reads
        delimiter = "\n" + match.group(2) + text[match.end() : name_end]
        body_end = text.find(delimiter, body_start)
        while body_end != -1 and text[body_end + len(delimiter) : body_end + len(delimiter) + 1] not in ("", "\n", "\r"):
            body_end = text.find(delimiter, body_end + 1)
        body_end = body_end if body_end != -1 else length
        if quoted:
            continue  # no expansion; a fetch-and-run inside is a plain pipe, caught as such
        # The same test as an inline substitution: an opener followed by a
        # fetch command, or a decoder inside the substitution.
        openers = scan.positions(_SUBST_OPEN)
        index = bisect.bisect_left(openers, body_start)
        while index < len(openers) and openers[index] < body_end:
            opener_at = openers[index]
            index += 1
            payload = _skip_to_next_word(text, opener_at + (1 if text[opener_at] == "`" else 2))
            if payload in fetcher_set:
                scan.add("fetch_and_run.command_substitution", base, start)
                break
            line_end = scan.line_end(opener_at)
            if _any_within(decoders, opener_at, line_end):
                scan.add("encoded_exec.decode_pipe", "critical", start)
                break


def _scan_base64_blobs(scan: _FileScan) -> None:
    """Long base64 runs in instruction prose (data: URIs excepted). Runs are
    collected as maximal bounded matches and merged, so this stays linear."""
    if PurePosixPath(scan.path).suffix.casefold() not in _PROSE_SUFFIXES:
        return
    text = scan.text
    if not _BASE64_GATE.search(text):
        return
    run_start = -1
    run_end = -1

    def flush() -> None:
        if run_start >= 0 and run_end - run_start >= _BASE64_MIN_RUN:
            prefix = text[max(0, run_start - 80) : run_start]
            if not _DATA_URI_TAIL.search(prefix):
                scan.add("encoded_exec.base64_blob", "high", run_start)

    for match in _BASE64_RUN.finditer(text):
        if match.start() == run_end:
            run_end = match.end()
            continue
        flush()
        run_start, run_end = match.start(), match.end()
    flush()


def _network_send(scan: _FileScan, sinks: list[int]) -> bool:
    """Does this file send data somewhere? (Escalates credential findings.)"""
    if sinks or (scan.may_match(_NETWORK_SEND) and _NETWORK_SEND.search(scan.text)):
        return True
    if scan.pairs(_SCP, _AT_SIGN):
        return True
    clients = scan.positions(_CURL_WGET)
    if not clients:
        return False
    for position in scan.positions(_UPLOAD_FLAG):
        if _last_before(clients, position, scan.logical_line_start(position)) is not None:
            return True
    return False


def _scan_credentials(scan: _FileScan, network: bool) -> None:
    text = scan.text
    for match in scan.finditer(_SENSITIVE_PATH):
        start = match.start()
        token = _PATH_TOKEN.match(text, start)
        token_text = token.group(0).casefold() if token else ""
        if token_text.endswith(".pub") or "known_hosts" in token_text:
            continue
        if text[match.end() : match.end() + 4].casefold() == ".pub":
            continue
        floor = scan.logical_line_start(start)
        end = scan.line_end(start)
        reads = _any_within(scan.positions(_READ_VERB), floor, end) or match.group(0).casefold().startswith("security")
        if reads:
            scan.add(
                "credential_access.sensitive_path_read", "critical" if network else "high", start
            )
        else:
            scan.add(
                "credential_access.sensitive_path_mention", "medium" if network else "low", start
            )

    for pattern in (_ENV_PIPE, _ENV_SUBST):
        for match in scan.finditer(pattern):
            start = match.start()
            floor = scan.logical_line_start(start)
            end = scan.line_end(start)
            if _any_within(scan.positions(_NETWORK_TOOL), floor, end):
                scan.add("credential_access.env_to_network", "critical", start)
    for match in scan.finditer(_ENV_DUMP):
        scan.add("credential_access.env_dump", "critical" if network else "high", match.start())


def _raw_ip_positions(scan: _FileScan) -> list[int]:
    positions = []
    for match in scan.finditer(_RAW_IP_URL):
        octets = tuple(int(group) for group in match.groups())
        if any(octet > 255 for octet in octets) or _is_private_ip(octets):
            continue
        positions.append(match.start())
        scan.charge(1, match.start())
    for match in scan.finditer(_NUMERIC_HOST_URL):
        positions.append(match.start())
        scan.charge(1, match.start())
    return sorted(positions)


def _scan_exfiltration(scan: _FileScan, hosts: list[int], raw_ips: list[int]) -> None:
    for position in hosts:
        scan.add("exfiltration.sink_host", "high", position)
    for position in raw_ips:
        scan.add("exfiltration.raw_ip_url", "high", position)
    for match in scan.finditer(_CHAT_WEBHOOK):
        scan.add("exfiltration.chat_webhook", "medium", match.start())
    for position in scan.pairs(_DNS_TOOL, _DOLLAR_PAREN):
        scan.add("exfiltration.dns_exfil", "high", position)


_STRUCTURED_SUFFIXES = {".json", ".yaml", ".yml", ".toml"}


def _bypass_severity(scan: _FileScan, match: re.Match[str]) -> str:
    """Critical where a tool APPLIES the setting (settings / MCP / hook
    configuration). Code and prose that merely name the flag (detectors,
    tests, docs) are high; a `Bash(*)` example in prose is medium."""
    applied = scan.context == "config" or (
        scan.context == "hook" and PurePosixPath(scan.path).suffix.casefold() in _STRUCTURED_SUFFIXES
    )
    if applied:
        return "critical"
    if match.group(0).casefold().startswith("bash(") and not scan.executable:
        return "medium"
    return "high"


def _scan_permissions_and_persistence(scan: _FileScan) -> None:
    text = scan.text
    executable = scan.executable
    for match in scan.finditer(_AUTO_APPROVE):
        scan.add(
            "permission_bypass.auto_approve_hook",
            _severity(executable, exec_sev="high", prose_sev="medium"),
            match.start(),
        )
    for match in scan.finditer(_SKIP_PERMISSIONS):
        scan.add("permission_bypass.skip_permissions", _bypass_severity(scan, match), match.start())
    for match in scan.finditer(_TOOL_SETTINGS_PATH):
        start = match.start()
        if _any_within(scan.positions(_WRITE_VERB), scan.logical_line_start(start), start):
            scan.add(
                "permission_bypass.tool_settings_write",
                _severity(executable, exec_sev="high", prose_sev="medium"),
                start,
            )
    persistence = _severity(executable, exec_sev="high", prose_sev="medium")
    for match in scan.finditer(_SHELL_RC):
        scan.add("persistence.shell_rc", persistence, match.start())
    for match in scan.finditer(_SCHEDULER):
        scan.add("persistence.scheduler", persistence, match.start())
    for match in scan.finditer(_GIT_HOOKS):
        start = match.start()
        floor = scan.logical_line_start(start)
        end = scan.line_end(start)
        if "hookspath" in match.group(0).casefold():
            if _HOOKS_PATH_DEV_NULL.match(text, match.end()):
                continue  # core.hooksPath=/dev/null DISABLES hooks
            scan.add("persistence.git_hooks", persistence, start)
            continue
        if (
            _any_within(scan.positions(_WRITE_VERB), floor, end)
            or _any_within(scan.positions(_CHMOD), floor, end)
        ):
            scan.add("persistence.git_hooks", persistence, start)


def _scan_concealment_and_evasion(scan: _FileScan) -> None:
    for match in scan.finditer(_HIDE):
        scan.add("concealment.hide_from_user", "medium", match.start())
        scan.concealment = True
    for match in scan.finditer(_OVERRIDE):
        scan.add("concealment.override_instructions", "medium", match.start())
        scan.concealment = True
    for match in scan.finditer(_DISABLE_SECURITY):
        scan.add("defense_evasion.disable_security", "high", match.start())
    for position in scan.pairs(_SET_MPPREFERENCE, _DISABLE_FLAG):
        scan.add("defense_evasion.disable_security", "high", position)
    for position in scan.pairs(_XATTR, _QUARANTINE_ATTR):
        scan.add("defense_evasion.quarantine_bypass", "medium", position)
    for match in scan.finditer(_SESSIONFS_TAMPER):
        scan.add("defense_evasion.sessionfs_tamper", "high", match.start())
    for position in scan.pairs(_RM_FORCE, _SESSIONFS_DIR) + scan.pairs(_LAUNCHCTL_STOP, _SESSIONFS_WORD):
        scan.add("defense_evasion.sessionfs_tamper", "high", position)
    for match in scan.finditer(_HISTORY_CLEAR):
        scan.add("defense_evasion.history_clear", "high", match.start())
    for position in scan.pairs(_RM, _SHELL_HISTORY):
        scan.add("defense_evasion.history_clear", "high", position)


def _comment_spans(text: str, scan: _FileScan | None = None) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    position = 0
    while True:
        start = text.find("<!--", position)
        if start == -1:
            return spans
        end = text.find("-->", start + 4)
        # An unterminated comment hides everything after it when rendered.
        stop = len(text) if end == -1 else end + 3
        spans.append((start, stop))
        if scan is not None:
            scan.charge(2, start)
        position = stop


def _scan_comments(scan: _FileScan) -> list[tuple[int, int]]:
    if PurePosixPath(scan.path).suffix.casefold() not in _MARKUP_SUFFIXES:
        return []
    spans = _comment_spans(scan.text, scan)
    keywords = scan.positions(_COMMENT_KEYWORDS) if spans else []
    for start, stop in spans:
        if _any_within(keywords, start + 4, stop):
            scan.add("hidden_content.html_comment", "medium", start)
    return spans


def _is_emoji(char: str) -> bool:
    code = ord(char)
    return code >= 0x1F000 or 0x2600 <= code <= 0x27BF or code in (0x2640, 0x2642, 0x2695)


def _scan_unicode(scan: _FileScan) -> None:
    """Hidden / bidi / control characters, on the RAW text."""
    raw = scan.text
    # A raw control byte (ESC, BEL, a C1 control ...) has no legitimate use in
    # a skill: scripts that colour output write the escape as TEXT (\033[,
    # \e[, \x1b[). Where the agent or a shell acts on the text - SKILL.md,
    # scripts, hooks, config - it is critical and refused at publish, whether
    # or not the command it might hide can be reconstructed. Elsewhere high.
    strict_control = (
        scan.context in {"script", "hook", "config"} or PurePosixPath(scan.path).name == "SKILL.md"
    )
    control = "critical" if strict_control else "high"
    for match in _CONTROL.finditer(raw):
        position = match.start()
        if raw[position] == "\x0c" and _alone_on_line(raw, position):
            continue  # a form feed on a line of its own: the page-break convention
        scan.add("unicode.control_character", control, position)
    if raw.isascii():
        return
    strict = scan.executable or PurePosixPath(scan.path).name == "SKILL.md"
    for match in _TAG_CHARS.finditer(raw):
        scan.add("unicode.tag_characters", "critical", match.start())
    for match in _BIDI_OVERRIDE.finditer(raw):
        scan.add("unicode.bidi_control", "critical" if strict else "medium", match.start())
    for match in _BIDI_ISOLATE.finditer(raw):
        scan.add("unicode.bidi_control", "medium", match.start())
    for match in _BIDI_MARK.finditer(raw):
        scan.add("unicode.bidi_mark", "low", match.start())  # never more than informational
    for match in _ZERO_WIDTH.finditer(raw):
        position = match.start()
        char = raw[position]
        if char == "﻿" and position == 0:
            continue  # byte-order mark at file start
        if (
            char == "\u200d"
            and 0 < position < len(raw) - 1
            and _is_emoji(raw[position - 1])
            and _is_emoji(raw[position + 1])
        ):
            continue  # zero-width joiner inside an emoji sequence
        if char in _JOINERS:
            # ZWNJ / ZWJ are ordinary in Persian, Arabic and Indic text. Matching
            # strips them first, so a split dangerous token is still caught.
            scan.add("unicode.zero_width", "low", position)
            continue
        scan.add("unicode.zero_width", "critical" if scan.executable else "medium", position)
    for match in _VARIATION.finditer(raw):
        length = match.end() - match.start()
        scan.add(
            "unicode.variation_selector_run", "critical" if length > 16 else "medium", match.start()
        )


def _alone_on_line(text: str, position: int) -> bool:
    """Only blanks share ``position``'s line (a form feed page break)."""
    start = position
    while start > 0 and text[start - 1] in " \t":
        start -= 1
    end = position + 1
    while end < len(text) and text[end] in " \t\r":
        end += 1
    return (start == 0 or text[start - 1] == "\n") and (end == len(text) or text[end] == "\n")


# ESC + one of these opens a control STRING (OSC, DCS, SOS, PM, APC) that
# runs to BEL (OSC) or the string terminator ESC \\ / U+009C.
_STRING_INTRODUCERS = frozenset("]PX^_")


@dataclass
class _Stripped:
    """Text with controls removed, and the offset map back to its input
    (same convention as _detection_text: physical = pos + totals[k - 1],
    k = number of points <= pos)."""

    text: str
    points: Sequence[int]
    totals: Sequence[int]
    # Some sequence reads differently when its content / final byte is kept.
    ambiguous: bool = False
    # The map is exact (False after a length-changing NFKC).
    exact: bool = True
    # Offset (in ``text``) where the rewrite stopped because MAX_EDITS was
    # reached; the member then fails closed (analysis budget).
    exhausted_at: int | None = None


def _strip_controls(text: str, *, keep_content: bool = False, invisible: bool = False) -> _Stripped:
    """Remove every C0 / C1 control except TAB, LF and CR, in one linear
    pass, with an offset map.

    Two readings of a terminal escape sequence:

    * terminal (default): what a terminal shows - a CSI (``ESC [ params
      final``) or a control string (``ESC ] ... BEL``, ``ESC P ... ESC \\``
      ...) renders as nothing and is removed whole;
    * raw (``keep_content``): what the agent reads - only the control bytes
      go; a CSI's final byte that is a letter or digit and a control
      string's content stay (``c\\x1b[url`` reads ``curl``,
      ``\\x1b]curl URL | sh\\x07`` reads ``curl URL | sh``).

    ``ambiguous`` says the two readings differ; the scanner then matches
    both and reports the union. Any other control is removed alone
    (``c\\x1burl`` reads ``curl``). U+009B / U+009D are single characters
    (UTF-8 terminals do not treat them as CSI / OSC). With ``invisible`` the
    invisible characters (zero-width, bidi, tags, variation selectors) are
    removed in the same pass, so one offset map covers both."""
    pattern = _REMOVED_CHARS if invisible else _REMOVED_CONTROLS
    if not pattern.search(text):
        return _Stripped(text, [], [])
    edits = _Edits()
    removed = 0
    output = 0
    last = 0
    ambiguous = False
    exhausted_at: int | None = None
    length = len(text)

    def cut(begin: int, stop: int, replacement: str = "") -> None:
        nonlocal removed, output, last, exhausted_at
        edits.piece(text[last:begin])
        edits.piece(replacement)
        output += begin - last + len(replacement)
        removed += stop - begin - len(replacement)
        if not edits.mark(output, removed) and exhausted_at is None:
            exhausted_at = output
        last = stop

    for match in pattern.finditer(text):
        if exhausted_at is not None:
            break  # edit budget spent: the member fails closed
        start = match.start()
        if start < last:
            # A run that began inside a sequence already handled (e.g. the
            # bytes after a BEL terminator): remove what lies past it.
            if match.end() > last:
                cut(last, match.end())
            continue
        end = start + 1
        char = text[start]
        introducer = text[end : end + 1]
        if char == "\x1b" and introducer == "[":
            end += 1
            while end < length and text[end] in _CSI_PARAMETERS:
                end += 1
            while end < length and text[end] in _CSI_INTERMEDIATES:
                end += 1
            final_alnum = end < length and text[end].isascii() and text[end].isalnum()
            if end < length and "@" <= text[end] <= "~":
                end += 1
            if final_alnum:
                ambiguous = True
                if keep_content:
                    cut(start, end - 1)  # keep the letter / digit
                    continue
            cut(start, end)
        elif char == "\x1b" and introducer in _STRING_INTRODUCERS:
            body = end + 1
            stop = body
            while stop < length and text[stop] not in "\x07\x1b\x9c\n":
                stop += 1
            if stop < length and text[stop] in "\x07\x9c":
                close = stop + 1
            elif text[stop : stop + 2] == "\x1b\\":
                close = stop + 2
            else:
                close = stop
            if stop > body:
                ambiguous = True
            if keep_content:
                # The content stays, set apart by a blank on each side so it
                # cannot glue onto neighbouring words (the terminal reading
                # covers a string placed INSIDE a word).
                cut(start, body, " ")
                if close > stop:
                    terminator_start = stop
                    for inner in pattern.finditer(text, body, stop):
                        if exhausted_at is not None:
                            break
                        cut(inner.start(), inner.end())
                    cut(terminator_start, close, " ")
                continue
            cut(start, close)
        elif char == "\x1b" and introducer == "\\":
            cut(start, end + 1)  # a stray string terminator
        else:
            cut(start, match.end())  # a run of controls / invisibles
    edits.piece(text[last:])
    stripped = _Stripped(edits.text(), edits.points, edits.totals, ambiguous, exhausted_at=exhausted_at)
    if exhausted_at is not None:
        return stripped
    if _leftover(stripped.text, invisible):
        # Invariant: no removed character survives. Should a sequence ever be
        # mis-scanned, fall back to removing every such character alone.
        return _strip_plain(text, ambiguous=ambiguous, invisible=invisible)
    return stripped


def _leftover(text: str, invisible: bool) -> bool:
    """Any control (except TAB, LF, CR) - or, with ``invisible``, invisible
    character - left in ``text``."""
    return bool((_REMOVED_CHARS if invisible else _REMOVED_CONTROLS).search(text))


def _strip_plain(text: str, *, ambiguous: bool, invisible: bool) -> _Stripped:
    """Every removed character dropped on its own, with the offset map."""
    pattern = _REMOVED_CHARS if invisible else _REMOVED_CONTROLS
    edits = _Edits()
    exhausted_at: int | None = None
    last = removed = output = 0
    for match in pattern.finditer(text):
        edits.piece(text[last : match.start()])
        output += match.start() - last
        removed += match.end() - match.start()
        last = match.end()
        if not edits.mark(output, removed):
            exhausted_at = output
            break
    edits.piece(text[last:])
    return _Stripped(edits.text(), edits.points, edits.totals, ambiguous, exhausted_at=exhausted_at)


def _map_back(points: Sequence[int], totals: Sequence[int], position: int) -> int:
    if not points:
        return position
    index = bisect.bisect_right(points, position)
    return position + totals[index - 1] if index else position


def _normalize_readings(raw: str) -> list[_Stripped]:
    """Pattern text(s) for ``raw``: controls (except TAB, LF, CR) and
    invisible characters removed, then NFKC. One reading normally; two when
    an escape sequence reads differently kept or removed (at most two per
    file). Each carries its offset map back to ``raw``; when NFKC changes the
    length the map is dropped (columns then count normalized characters)."""
    ascii_only = raw.isascii()
    readings = [_strip_controls(raw, invisible=not ascii_only)]
    if readings[0].ambiguous:
        readings.append(_strip_controls(raw, keep_content=True, invisible=not ascii_only))
    if ascii_only:
        return readings
    result: list[_Stripped] = []
    for reading in readings:
        normalized = unicodedata.normalize("NFKC", reading.text)
        if len(normalized) == len(reading.text):
            result.append(_Stripped(
                normalized, reading.points, reading.totals, reading.ambiguous, True, reading.exhausted_at
            ))
        else:
            # Compatibility expansion moved offsets: no exact map.
            result.append(_Stripped(normalized, [], [], reading.ambiguous, False, reading.exhausted_at))
    return result


def _normalize(raw: str) -> str:
    """The terminal reading's pattern text (see _normalize_readings)."""
    return _normalize_readings(raw)[0].text


# In prose a command may follow any number of leading words of a sentence
# or list / heading / prompt marker ("Install it with curl URL | sh"); past
# the first word only a plain command-name-shaped word can start it.
_NON_PLAIN = re.compile(r"[^A-Za-z0-9_./~+-]")
_PROMPT_MARKERS = frozenset({"$", "%", "#", ">>>", "PS>", "❯", "➜", "λ"})

# Shell analysis budget (characters of statements lexed). A package whose
# files pack more fetch / decode statements than this is refused (fail
# closed) rather than analyzed for minutes: no legitimate skill needs it.
LEX_FILE_BUDGET = 1024 * 1024
LEX_PACKAGE_BUDGET = 4 * 1024 * 1024
# Items one member's analysis may collect (match positions of every pattern,
# comment spans): the same budget bounds the intermediate work, so a member
# of repeated tokens (`curl curl curl ...`) stops early and fails closed
# instead of building position lists and parse trees of millions of entries.
# No legitimate member comes near it (the official plugins peak far below).
MAX_SCAN_ITEMS = 1 << 20
# `<&N` descriptor reads one member may hold (each may need its statement
# lexed); past it the member fails closed (analysis budget).
MAX_FD_READS = 1 << 16


@dataclass
class _LexBudget:
    remaining: int = LEX_PACKAGE_BUDGET


def _scan_patterns(
    path: str,
    raw: str,
    budget: _LexBudget | None = None,
    reading: _Stripped | None = None,
    depth: int = 0,
    shell: bool = False,
) -> _FileScan:
    if reading is None:
        reading = _normalize_readings(raw)[0]
    normalized = reading.text
    exact = reading.exact
    exhausted_at = reading.exhausted_at
    detection: str = normalized
    points: Sequence[int] = []
    totals: Sequence[int] = []
    if exhausted_at is None:
        try:
            detection, points, totals = _detection_text(normalized)
        except _AnalysisExhausted as exhausted:
            exhausted_at = exhausted.position
    scan = _FileScan(
        path=path,
        context=classify_path(path),
        text=detection,
        shell=shell or depth > 0,
        line_text=normalized,
        map_points=points,
        map_totals=totals,
        raw_text=raw if exact else None,
        raw_points=reading.points,
        raw_totals=reading.totals,
    )
    spans: list[tuple[int, int]] = []
    if exhausted_at is None:
        try:
            hosts = scan.positions(_SINK_HOST)
            raw_ips = _raw_ip_positions(scan)
            sinks = sorted(hosts + raw_ips)
            _scan_fetch_and_encoded(scan, sinks, budget or _LexBudget(), depth)
            _scan_base64_blobs(scan)
            _scan_credentials(scan, _network_send(scan, sinks))
            _scan_exfiltration(scan, hosts, raw_ips)
            _scan_permissions_and_persistence(scan)
            _scan_concealment_and_evasion(scan)
            spans = _scan_comments(scan)
        except _AnalysisExhausted as exhausted:
            exhausted_at = exhausted.position
            spans = []
    if exhausted_at is not None:
        # Too many tokens, spans or edits to analyze within the budget: fail
        # closed, keeping the findings made before the budget ran out.
        scan.add("hidden_content.analysis_budget", "critical", min(exhausted_at, len(scan.text)))
    # Only locate() is needed from here on: drop the per-rule work.
    scan._positions = {}
    scan._lower = None
    scan.statement_starts = {}
    if spans:
        starts = [start for start, _ in spans]
        for hit in scan.hits:
            index = bisect.bisect_right(starts, hit.position) - 1
            if (
                index >= 0
                and hit.position < spans[index][1]
                and hit.rule_id != "hidden_content.html_comment"
            ):
                hit.in_comment = True
    return scan


def _group_key(path: str) -> str:
    """Escalation scope: one skill directory (skills/<name>/...); any file
    outside a skill directory is its own scope."""
    parts = PurePosixPath(path).parts
    if len(parts) >= 3 and parts[0] == "skills":
        return "/".join(parts[:2])
    return path


def scan_files(
    files: Iterable[tuple[str, str]], binary_executables: Iterable[str] = ()
) -> ScanResult:
    """Scan (package-relative path, decoded text) pairs. ``binary_executables``
    are paths of members in an executable context that are not text at all:
    each is a critical finding (refused at publish)."""
    scanned: list[tuple[_FileScan, _FileScan]] = []
    concealed_groups: set[str] = set()
    dangerous_groups: set[str] = set()
    budget = _LexBudget()
    members = list(files)
    shell_run = _members_run_by_a_shell(members)
    for path, raw in members:
        hidden = _FileScan(path=path, context=classify_path(path), text=raw)
        _scan_unicode(hidden)
        # One pattern scan per reading (two only when an escape sequence
        # reads differently kept or removed); findings are the union.
        for reading in _normalize_readings(raw):
            patterns = _scan_patterns(path, raw, budget, reading, shell=path in shell_run)
            if patterns.concealment:
                concealed_groups.add(_group_key(path))
            if any(
                RULES[hit.rule_id].family in _DANGEROUS_FAMILIES
                and SEVERITY_RANK[hit.severity] >= SEVERITY_RANK["medium"]
                for hit in patterns.hits
            ):
                dangerous_groups.add(_group_key(path))
            scanned.append((patterns, hidden))

    findings: list[ScanFinding] = []
    truncated = False
    seen_hidden: set[int] = set()
    for patterns, hidden in scanned:
        group = _group_key(patterns.path)
        # Concealment AND a dangerous instruction in the same scope.
        concealed = group in concealed_groups and group in dangerous_groups
        truncated = truncated or patterns.capped or hidden.capped
        sources = [patterns] if id(hidden) in seen_hidden else [patterns, hidden]
        seen_hidden.add(id(hidden))
        for source in sources:
            for hit in source.hits:
                rule = RULES[hit.rule_id]
                severity = hit.severity
                if concealed:
                    # The same skill tells the agent to hide what it does:
                    # concealment itself is high, and anything dangerous it
                    # could be hiding is critical.
                    if rule.family == "concealment":
                        severity = "high"
                    elif (
                        rule.family in _DANGEROUS_FAMILIES
                        and SEVERITY_RANK[severity] >= SEVERITY_RANK["medium"]
                    ):
                        severity = "critical"
                if hit.in_comment and (
                    SEVERITY_RANK[severity] >= SEVERITY_RANK["high"] or rule.family == "concealment"
                ):
                    # Invisible when rendered, read by the agent: a hidden
                    # override or dangerous step is a prompt injection.
                    severity = "critical"
                line, column, line_sha = source.locate(hit.position)
                findings.append(
                    ScanFinding(
                        rule_id=rule.rule_id,
                        family=rule.family,
                        severity=severity,
                        path=patterns.path,
                        line=line,
                        column=column,
                        message=rule.explanation,
                        line_sha=line_sha,
                    )
                )
    binary_rule = RULES["hidden_content.binary_in_executable_context"]
    binary_paths = sorted(set(binary_executables))
    for binary_path in binary_paths:
        findings.append(
            ScanFinding(
                rule_id=binary_rule.rule_id,
                family=binary_rule.family,
                severity="critical",
                path=binary_path,
                line=1,
                column=1,
                message=binary_rule.explanation,
                line_sha=hashlib.sha256(b"").hexdigest(),
            )
        )
    findings.sort(
        key=lambda item: (-SEVERITY_RANK[item.severity], item.path, item.line, item.column, item.rule_id)
    )
    # Two readings of one file report the same hit twice: keep the worst.
    unique: dict[tuple[str, str, int, int], ScanFinding] = {}
    for finding in findings:
        unique.setdefault((finding.path, finding.rule_id, finding.line, finding.column), finding)
    findings = list(unique.values())
    if len(findings) > MAX_FINDINGS:
        # Worst first, so truncation never drops a critical in favour of a low.
        findings = findings[:MAX_FINDINGS]
        truncated = True
    findings.sort(key=lambda item: (item.path, item.line, item.column, item.rule_id))
    return ScanResult(
        ruleset_version=RULESET_VERSION,
        findings=tuple(findings),
        scanned_files=len(seen_hidden) + len(binary_paths),
        truncated=truncated,
    )


# Share of undecodable / control characters above which a member is
# genuinely not text (compiled code, images): below it the member is text a
# shell or agent reads, and its stray bytes are findings of their own.
_BINARY_SHARE = 0.3
_NOT_TEXT = re.compile("[\ufffd\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _mostly_text(text: str) -> bool:
    if not text:
        return True
    odd = sum(1 for _ in _NOT_TEXT.finditer(text))
    return odd / len(text) < _BINARY_SHARE


def _executable_context(relative: str) -> bool:
    """Where content is acted on: scripts, hooks, executable config (anything
    the package model treats as runnable) and SKILL.md."""
    return classify_path(relative) in EXEC_CONTEXTS or PurePosixPath(relative).name.casefold() == "skill.md"


def scan_tree(root: Path) -> ScanResult:
    """Scan every member of a package directory.

    Text members are scanned. A member that LOOKS binary (a NUL or a binary
    magic, the publish gate's predicate) is skipped only outside executable
    contexts (images, archives under assets/...). In an executable context it
    is a shell / agent input whatever its bytes: if it is mostly text (bash
    ignores NUL; `MZ` or a stray magic can start a script) it is scanned -
    its control / undecodable bytes are then critical by policy - and if it
    is genuinely not text it is reported
    ``hidden_content.binary_in_executable_context`` (critical)."""
    from sessionfs.skills.package import looks_binary

    files: list[tuple[str, str]] = []
    binaries: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        raw = path.read_bytes()
        relative = path.relative_to(root).as_posix()
        if looks_binary(raw):
            if not _executable_context(relative):
                continue  # a genuine asset where nothing runs it
            text = raw.decode("utf-8", errors="replace")
            if _mostly_text(text):
                files.append((relative, text))
            else:
                binaries.append(relative)
            continue
        files.append((relative, raw.decode("utf-8", errors="replace")))
    return scan_files(files, binaries)


# The shell lexer's token pattern is linted with this module's.
_SHELL_TOKEN = _SHELL_PATTERNS[0]
_SHELL_NEWLINE = _SHELL_PATTERNS[1]

# Every compiled pattern in this module (the lint test walks these).
ALL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    value for _name, value in sorted(globals().items()) if isinstance(value, re.Pattern)
)
