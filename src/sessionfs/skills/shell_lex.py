"""Bounded, linear, quote-aware shell lexer for the instruction scanner.

Splits one statement (a logical line, continued across a line break only
after ``|``, ``&&`` or ``||``) into command lists, pipelines and commands:

* quotes (single, double) and backslash escapes - in shell code a quoted
  word may span line breaks, as in the shell; in prose a quote never does
  (an unterminated quote is a literal character), so a stray apostrophe
  cannot swallow the rest of a document;
* comments (``#`` at the start of a word, only when ``comments`` is set -
  in markdown a ``#`` line is a heading the agent reads);
* the operators ``;`` ``&&`` ``||`` ``|`` ``|&`` ``&`` and line breaks;
* redirections (``<`` ``<<<`` ``<<`` ``<<-`` ``<&N`` ``>`` ``>>`` ...);
* ``$(...)``, backticks, ``<(...)``, ``>(...)`` and ``( ... )`` subshells as
  nested command lists, up to ``MAX_DEPTH`` levels;
* compound commands - brace groups ``{ ...; }``, ``if ... fi``, ``for`` /
  ``while`` / ``until`` / ``select ... done`` and ``case ... esac`` - as
  nested command lists too (``Command.subshell``), so a group is ONE pipeline
  stage summarizing what it holds (``{ curl URL; } | sh``,
  ``curl URL | while read l; do sh -c "$l"; done``). A group not closed on
  its line ends there; a closing word with no opening one in the statement
  (``done | sh`` after a multi-line loop) is an ``opaque`` stage - the output
  of a compound the statement does not show.

The content of every quoted string is ALSO parsed as a nested command list
(``echo 'curl URL | sh' >> ~/.bashrc`` writes a fetch-and-run; a quoted
argument such as ``"https://x/?a=1;b=2"`` is one word to its own command).

Every character is consumed once per nesting level (at most MAX_DEPTH + 1
times) and token matching is a single bounded regex at each position. There
are NO fixed look-back / look-ahead windows: padding (spaces, ``>``, blank
lines, long quoted or backtick spans) can never change how text parses. The
searches that need the next ``\n`` / quote / backtick / ``}`` / ``))`` use a
per-character "next occurrence" cache, so repeated searches stay linear, and
the line-leading test uses per-line results computed once.
"""

from __future__ import annotations

import bisect
import re
from array import array
from dataclasses import dataclass, field

MAX_DEPTH = 4
MAX_TOKENS = 500_000
# Stands for a substitution inside a word's value (``"$(curl ...)"``).
SUB_MARK = "\x01"

_TOKEN = re.compile(
    r"(?P<ws>[ \t\r]{1,400})"
    r"|(?P<nl>\n)"
    r"|(?P<sub>\$\(\(|\$\(|<\(|>\(|`{1,8})"
    r"|(?P<andor>&&|\|\|)"
    r"|(?P<redir>[0-9]{0,2}(?:<<<|<<-|<<|<&|<>|&>>|&>|>>|>&|>\||<|>))"
    r"|(?P<op>\|&|;;|[|;&])"
    r"|(?P<close>\))"
    r"|(?P<open>\()"
    r"|(?P<quote>['\"])"
    r"|(?P<esc>\\[\s\S])"
    r"|(?P<brace>\$\{)"
    r"|(?P<plain>[^\s|;&<>()`'\"\\$]{1,400}|\$|\\)"
)
_NEWLINE = re.compile("\n")
PATTERNS = (_TOKEN, _NEWLINE)

# Reserved words and group delimiters that prefix or terminate a compound
# command's simple command. Skipped at the front of a command so a control
# structure's head does not hide the fetch it guards (``if curl URL | sh``,
# ``! curl URL | sh``, ``{ curl URL | sh; }``, ``function NAME { ... }``).
# ``!`` / ``{`` / ``}`` are single characters handled separately: a ``{``
# glued to a word is a brace word and a ``!`` glued to a word is history
# expansion, not a delimiter. ``function`` is a reserved word as well, so the
# name of a ``function NAME { ... }`` definition becomes the stage head.
_CONTROL_WORD = re.compile(
    r"(?i)(?:if|then|elif|else|fi|for|while|until|do|done|in|select|case|esac|function)\b"
)
# Compound commands parsed as groups: opening word -> closing word. Reserved
# words are case-sensitive in the shell (``If`` in prose is not one).
GROUP_CLOSERS = {
    "{": "}", "if": "fi", "for": "done", "while": "done", "until": "done", "select": "done",
    "case": "esac",
}
CLOSER_WORDS = frozenset(GROUP_CLOSERS.values())
# Compound groups nest independently of MAX_DEPTH (a group is consumed once,
# like a subshell, so nesting does not multiply the work); past this depth an
# opening word is skipped as before and its closer becomes an opaque stage.
MAX_GROUP_DEPTH = 32
_WORD_END = frozenset(" \t\r\n;|&()<>")


@dataclass
class Sub:
    kind: str  # "$(" | "`" | "<(" | ">("
    start: int
    end: int
    body: CommandList | None


@dataclass
class Word:
    value: str
    start: int
    end: int
    subs: list[Sub] = field(default_factory=list)
    nested: list[CommandList] = field(default_factory=list)


@dataclass
class Redirect:
    op: str
    target: Word | None
    start: int


@dataclass
class Command:
    words: list[Word]
    redirects: list[Redirect]
    start: int
    end: int
    subshell: CommandList | None = None
    # A closing word (``}``, ``fi``, ``done``, ``esac``) whose compound
    # opened outside this statement: stands for that compound's output.
    opaque: bool = False


@dataclass
class Pipeline:
    commands: list[Command]
    start: int
    end: int


@dataclass
class CommandList:
    pipelines: list[Pipeline]
    separators: list[str]  # after each pipeline: ";", "&&", "||", "&", "\n" or ""


class LineIndex:
    """Line-break offsets of a text in bounded memory.

    A list of every ``\\n`` offset costs ~36 bytes per line (a 16 MiB member
    of short lines would need hundreds of MiB). This keeps one cumulative
    count per BLOCK characters (16 K counts for 16 MiB) and finds the breaks
    inside a block on demand; a few recently used blocks stay cached, so the
    forward-moving lookups the scanner makes cost one block scan each."""

    BLOCK = 1024
    _CACHED_BLOCKS = 64

    def __init__(self, text: str) -> None:
        self.text = text
        block = self.BLOCK
        counts = array("q", [0])
        total = 0
        for start in range(0, len(text), block):
            total += text.count("\n", start, start + block)
            counts.append(total)
        self.counts = counts
        self.total = total
        self._blocks: dict[int, list[int]] = {}

    def before(self, position: int) -> int:
        """Number of line breaks in ``text[:position]``."""
        position = max(0, min(position, len(self.text)))
        block = position // self.BLOCK
        return self.counts[block] + self.text.count("\n", block * self.BLOCK, position)

    def nth(self, index: int) -> int:
        """Offset of the ``index``-th line break (0-based), ``len(text)``
        when there is none."""
        if index >= self.total or index < 0:
            return len(self.text)
        block = bisect.bisect_right(self.counts, index) - 1
        breaks = self._blocks.get(block)
        if breaks is None:
            if len(self._blocks) >= self._CACHED_BLOCKS:
                self._blocks.clear()
            start = block * self.BLOCK
            breaks = [match.start() for match in _NEWLINE.finditer(self.text, start, start + self.BLOCK)]
            self._blocks[block] = breaks
        return breaks[index - self.counts[block]]

    def line_start(self, position: int) -> int:
        """Start of the line holding ``position``."""
        count = self.before(position)
        return self.nth(count - 1) + 1 if count else 0

    def line_end(self, position: int) -> int:
        """First line break at or after ``position`` (``len(text)`` if none)."""
        return self.nth(self.before(position))


class _Budget(Exception):
    pass


class ShellLexer:
    def __init__(
        self, text: str, *, comments: bool, interesting: list[int] | None = None, prose: bool = False
    ) -> None:
        """``interesting``: sorted offsets (fetch / decode commands). A quoted
        string is parsed as a nested command list only when one lies inside
        it - nothing else in quoted text can matter to the rules. ``prose``:
        markdown / text, where a line-leading ``>`` is a blockquote marker."""
        self.text = text
        self.comments = comments
        self.prose = prose
        self.tokens = 0
        self.budget = MAX_TOKENS
        self.interesting = interesting
        # char -> (searched_from, found): ``found`` is the first occurrence at
        # or after every position in [searched_from, found] (len(text) = none).
        self._next_cache: dict[str, tuple[int, int]] = {}
        self._lines: LineIndex | None = None
        # Offset of a substitution / subshell nested past MAX_DEPTH in the
        # last statement (not parsed: the caller must fail closed), or None.
        self.too_deep: int | None = None
        # line start -> end of that line's leading run of blanks and ">".
        self._lead_end: dict[int, int] = {}
        self.group_depth = 0

    # -- public ---------------------------------------------------------------
    def statement(self, start: int, stop: int | None = None) -> tuple[CommandList, int]:
        """Parse the statement starting at ``start``; returns (list, end).

        The token budget is per statement and proportional to the text after
        ``start`` (each character is tokenized at most once per nesting
        level), so it is a safety net against a lexer bug, never a limit an
        input can reach to hide what follows it."""
        limit = len(self.text) if stop is None else min(stop, len(self.text))
        self.tokens = 0
        self.too_deep = None
        self.group_depth = 0
        self.budget = (limit - start + 1) * (MAX_DEPTH + 2) + 1024
        try:
            return self._list(start, limit, 0, None)
        except _Budget:
            return CommandList([], []), limit

    # -- helpers --------------------------------------------------------------
    def _match(self, pos: int, end: int) -> re.Match[str] | None:
        self.tokens += 1
        if self.tokens > self.budget:
            raise _Budget
        return _TOKEN.match(self.text, pos, end)

    def _next(self, char: str, pos: int) -> int:
        """First index >= pos holding ``char`` (len(text) if none). Cached so
        a run of searches over the same span costs one scan."""
        cached = self._next_cache.get(char)
        if cached is not None and cached[0] <= pos <= cached[1]:
            return cached[1]
        found = self.text.find(char, pos)
        if found == -1:
            found = len(self.text)
        self._next_cache[char] = (pos, found)
        return found

    def _find(self, char: str, pos: int, end: int) -> int:
        found = self._next(char, pos)
        return found if found < end else -1

    def _line_leading(self, pos: int) -> bool:
        """Only blanks and ``>`` precede ``pos`` on its line, however many.
        The line start comes from a bounded line index (LineIndex); each line's
        leading run is measured once."""
        if self._lines is None:
            self._lines = LineIndex(self.text)
        line_start = self._lines.line_start(pos)
        lead_end = self._lead_end.get(line_start)
        if lead_end is None:
            lead_end = line_start
            text = self.text
            length = len(text)
            while lead_end < length and text[lead_end] in " \t>":
                lead_end += 1
            self._lead_end[line_start] = lead_end
        return pos < lead_end

    def _line_end(self, pos: int, end: int) -> int:
        found = self._next("\n", pos)
        return found if found < end else end

    def _skip_blank(self, pos: int, end: int, newlines: bool) -> int:
        if pos >= end or self.text[pos] not in " \t\r\n":
            return pos  # fast path: nothing to skip
        # Any number of blanks (and, after `|` / `&&` / `||`, line breaks):
        # each character is skipped once, so this is linear.
        blanks = " \t\r\n" if newlines else " \t\r"
        text = self.text
        while pos < end and text[pos] in blanks:
            pos += 1
        return pos

    def _interesting(self, low: int, high: int) -> bool:
        if self.interesting is None:
            return True
        index = bisect.bisect_left(self.interesting, low)
        return index < len(self.interesting) and self.interesting[index] < high

    # -- grammar --------------------------------------------------------------
    def _list(
        self, pos: int, end: int, depth: int, closer: str | None, group: bool = False
    ) -> tuple[CommandList, int]:
        """closer None: a statement (a bare line break ends it); ")": until
        the matching parenthesis; "}": until the matching brace; "eof": the
        whole span (line breaks separate). ``group``: a compound command's
        body (``closer`` is its closing word, see GROUP_CLOSERS): until that
        word at a command start, or the end of the line when it is not
        closed there."""
        pipelines: list[Pipeline] = []
        separators: list[str] = []
        text = self.text
        while pos < end:
            pos = self._skip_blank(pos, end, newlines=False)
            if pos >= end:
                break
            char = text[pos]
            if char == ")" and closer == ")":
                return CommandList(pipelines, separators), pos + 1
            if group:
                assert closer is not None
                if self._word_at(pos, end, closer):
                    return CommandList(pipelines, separators), pos + len(closer)
                if char == ")" and closer != "esac":
                    # Closes an enclosing ``(`` / ``$(`` (a ``case`` pattern
                    # ends in ``)`` and stays inside its group).
                    return CommandList(pipelines, separators), pos
            elif char == "}" and closer == "}":
                return CommandList(pipelines, separators), pos + 1
            if char == "\n":
                if closer is None or group:
                    return CommandList(pipelines, separators), pos
                pos += 1
                continue
            before = pos
            pipeline, pos = self._pipeline(pos, end, depth, closer)
            if pipeline.commands:
                pipelines.append(pipeline)
                separators.append("")
            pos = self._skip_blank(pos, end, newlines=False)
            match = self._match(pos, end) if pos < end else None
            kind = match.lastgroup if match else None
            if match is not None and kind in ("andor", "op") and match.group(0) not in ("|", "|&"):
                operator = match.group(0)
                if separators:
                    separators[-1] = operator
                pos = match.end()
                if operator in ("&&", "||"):
                    pos = self._skip_blank(pos, end, newlines=True)
                continue
            if pos == before:
                pos += 1  # stray character (e.g. an unmatched ")"): always progress
        return CommandList(pipelines, separators), pos

    def _pipeline(self, pos: int, end: int, depth: int, closer: str | None) -> tuple[Pipeline, int]:
        start = pos
        commands: list[Command] = []
        while True:
            command, pos = self._command(pos, end, depth, closer)
            if command.words or command.redirects or command.subshell is not None or command.opaque:
                commands.append(command)
            after = self._skip_blank(pos, end, newlines=False)
            if after < end and self.text[after] == "|" and self.text[after : after + 2] != "||":
                width = 2 if self.text[after : after + 2] == "|&" else 1
                pos = self._skip_blank(after + width, end, newlines=True)
                continue
            return Pipeline(commands, start, pos), pos

    def _word_at(self, pos: int, end: int, word: str) -> bool:
        """``word`` stands alone at ``pos`` (followed by a blank, an operator
        or the end)."""
        after = pos + len(word)
        return self.text.startswith(word, pos) and (after >= end or self.text[after] in _WORD_END)

    def _opener(self, pos: int, end: int) -> str | None:
        """The compound command opening at ``pos`` (see GROUP_CLOSERS), when
        groups may still nest; else None."""
        if self.group_depth >= MAX_GROUP_DEPTH:
            return None
        text = self.text
        for opener in GROUP_CLOSERS:
            if text.startswith(opener, pos):
                after = pos + len(opener)
                # Followed by a blank: ``{a,b}`` is a brace word, ``for((``
                # an arithmetic loop, ``if`` alone at a line end no group.
                if after < end and text[after] in " \t":
                    return opener
        return None

    def _skip_control(self, pos: int, end: int) -> tuple[int, bool]:
        """Skip a run of reserved words / group delimiters / ``!`` at the
        front of a simple command (see _CONTROL_WORD): the following simple
        command's head becomes the stage head. Stops at a compound command
        that opens a group (see _opener). Returns (position, whether a
        closing word - see CLOSER_WORDS - was skipped). Unbounded but linear -
        every iteration advances ``pos``, so ``end`` (and the lexer token
        budget) guarantee termination."""
        closed = False
        while True:
            pos = self._skip_blank(pos, end, newlines=False)
            if pos >= end:
                return pos, closed
            if self._opener(pos, end) is not None:
                return pos, closed
            char = self.text[pos]
            if char in "{}!":
                after = pos + 1
                # A group delimiter / negation stands alone; ``{a,b}`` is a
                # brace word and ``!foo`` is history expansion.
                if after < end and self.text[after] not in " \t\r\n|;&)}":
                    return pos, closed
                closed = closed or char == "}"
                pos = after
                continue
            if self._word_at(pos, end, "time"):
                # ``time [-p] { ...; }``: the timed compound is the stage.
                after = self._skip_blank(pos + 4, end, newlines=False)
                while self._word_at(after, end, "-p") or self._word_at(after, end, "--"):
                    after = self._skip_blank(after + 2, end, newlines=False)
                if self._opener(after, end) is not None:
                    pos = after
                    continue
            match = _CONTROL_WORD.match(self.text, pos)
            if match is None:
                return pos, closed
            closed = closed or match.group(0) in CLOSER_WORDS
            pos = match.end()

    def _command(self, pos: int, end: int, depth: int, closer: str | None) -> tuple[Command, int]:
        pos, closed = self._skip_control(pos, end)
        start = pos
        words: list[Word] = []
        redirects: list[Redirect] = []
        subshell: CommandList | None = None
        group = False
        text = self.text
        opener = self._opener(pos, end)
        if opener is not None:
            # A compound command: its body is one nested list (one stage).
            self.group_depth += 1
            try:
                subshell, pos = self._list(pos + len(opener), end, depth, GROUP_CLOSERS[opener], group=True)
            finally:
                self.group_depth -= 1
            group = True
        while pos < end:
            pos = self._skip_blank(pos, end, newlines=False)
            if pos >= end:
                break
            char = text[pos]
            if char == "\n" or char in "|;&" and not text.startswith("&>", pos):
                break
            if char == ")":
                break
            if group and text[pos] not in "<>0123456789&":
                break  # after a compound only redirections belong to it
            if (
                char == "{"
                and words
                and not redirects
                and subshell is None
                and (pos + 1 >= end or text[pos + 1] in " \t\r\n|;&)}")
            ):
                # A function body (``function NAME { ... }``; ``function`` is
                # a control word, so ``words`` holds the name): parse the body
                # as its own command list, exactly as the ``NAME() { ... }``
                # form already splits the body into its own pipeline.
                if depth >= MAX_DEPTH:
                    if self.too_deep is None:
                        self.too_deep = pos
                    pos += 1
                    continue
                subshell, pos = self._list(pos + 1, end, depth + 1, "}")
                break
            if self.comments and char == "#":
                pos = self._line_end(pos, end)
                break
            if self.prose and char == ">" and not words and not redirects and self._line_leading(pos):
                pos += 1  # a markdown blockquote marker, not a redirect
                continue
            match = self._match(pos, end)
            if match is None:
                pos += 1
                continue
            kind = match.lastgroup
            if kind == "redir":
                operator = match.group(0)
                pos = self._skip_blank(match.end(), end, newlines=False)
                target, pos = self._word(pos, end, depth)
                redirects.append(Redirect(operator, target, match.start()))
                continue
            if kind == "open" and not words and subshell is None:
                if depth >= MAX_DEPTH:
                    if self.too_deep is None:
                        self.too_deep = match.start()
                    pos = match.end()
                    continue
                subshell, pos = self._list(match.end(), end, depth + 1, ")")
                continue
            word, new_pos = self._word(pos, end, depth)
            if word is None or new_pos <= pos:
                pos = max(new_pos, pos + 1)
                continue
            words.append(word)
            pos = new_pos
        opaque = closed and not words and subshell is None
        return Command(words, redirects, start, pos, subshell, opaque), pos

    def _word(self, pos: int, end: int, depth: int) -> tuple[Word | None, int]:
        text = self.text
        start = pos
        value: list[str] = []
        subs: list[Sub] = []
        nested: list[CommandList] = []
        while pos < end:
            char = text[pos]
            if char in " \t\r\n|;&)" and not (char == "&" and text.startswith("&>", pos)):
                break
            match = self._match(pos, end)
            if match is None:
                value.append(char)
                pos += 1
                continue
            kind = match.lastgroup
            token = match.group(0)
            if kind in ("ws", "nl", "andor", "op", "close", "redir"):
                if kind == "redir" and token.startswith(("<(", ">(")):
                    pass
                break
            if kind == "sub":
                pos = self._substitution(match, end, depth, value, subs)
                continue
            if kind == "quote":
                pos = self._quoted(pos, end, depth, token, value, subs, nested)
                continue
            if kind == "esc":
                if token[1] != "\n":
                    value.append(token[1])
                pos = match.end()
                continue
            if kind == "brace":
                close = self._find("}", pos + 2, self._line_end(pos, end))
                if close == -1:
                    value.append("$")
                    pos += 1
                    continue
                value.append(text[pos : close + 1])
                pos = close + 1
                continue
            # plain, open (inside a word: literal)
            value.append(token)
            pos = match.end()
        if pos == start:
            return None, pos
        return Word("".join(value), start, pos, subs, nested), pos

    def _substitution(
        self, match: re.Match[str], end: int, depth: int, value: list[str], subs: list[Sub]
    ) -> int:
        text = self.text
        token = match.group(0)
        start = match.start()
        if token == "$((":
            line_end = self._line_end(start, end)
            close = self._find(")", match.end(), line_end)
            while close != -1 and text[close + 1 : close + 2] != ")":
                close = self._find(")", close + 1, line_end)
            stop = close + 2 if close != -1 else match.end()
            value.append(text[start:stop])
            return stop
        if token.startswith("`") and len(token) > 1:
            value.append(token)  # `` or a markdown fence: literal
            return match.end()
        if depth >= MAX_DEPTH:
            if self.too_deep is None:
                self.too_deep = start
            value.append(token)
            return match.end()
        if token == "`":
            close = start + 1
            while True:
                close = self._find("`", close, end)
                if close == -1 or text[close - 1] != "\\":
                    break
                close += 1
            if close == -1:
                value.append(token)
                return match.end()
            body, _ = self._list(start + 1, close, depth + 1, "eof")
            subs.append(Sub("`", start, close + 1, body))
            value.append(SUB_MARK)
            return close + 1
        body, stop = self._list(match.end(), end, depth + 1, ")")
        subs.append(Sub(token, start, stop, body))
        value.append(SUB_MARK)
        return stop

    def _quoted(
        self,
        pos: int,
        end: int,
        depth: int,
        quote: str,
        value: list[str],
        subs: list[Sub],
        nested: list[CommandList],
    ) -> int:
        text = self.text
        # In shell code a quoted word may span lines (`bash -c 'a |<newline>b'`
        # runs both); in prose an apostrophe must not swallow the document,
        # so a quote there closes on its own line or is a literal.
        line_end = self._line_end(pos + 1, end) if self.prose else end
        close = pos + 1
        while True:
            close = self._find(quote, close, line_end)
            if close == -1 or quote == "'" or text[close - 1] != "\\":
                break
            close += 1
        if close == -1:
            return pos + 1  # unterminated on this line: the quote is dropped
        content = text[pos + 1 : close]
        if quote == '"':
            value.append(content.replace('\\"', '"').replace("\\\\", "\\"))
            if "$(" in content or "`" in content:
                value.append(SUB_MARK)
        else:
            value.append(content)
        if depth < MAX_DEPTH and len(content) >= 3 and self._interesting(pos + 1, close):
            parsed, _ = self._list(pos + 1, close, depth + 1, "eof")
            nested.append(parsed)
            if quote == '"':
                for pipeline in parsed.pipelines:
                    for command in pipeline.commands:
                        for word in command.words:
                            subs.extend(word.subs)
        return close + 1


def walk_lists(root: CommandList) -> list[tuple[CommandList, bool]]:
    """Every command list in a tree (root first) with ``quoted``: True for
    the parse of a quoted string's content (text, not executed by itself),
    False for lists the shell runs (the statement, substitution bodies,
    subshells). Each list is visited once; a substitution inside a double
    quoted string counts as executed (it runs when the command runs)."""
    found: list[tuple[CommandList, bool]] = []
    seen: set[int] = set()
    stack: list[tuple[CommandList, bool]] = [(root, False)]
    while stack:
        current, quoted = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        found.append((current, quoted))
        for pipeline in current.pipelines:
            for command in pipeline.commands:
                words = list(command.words)
                words.extend(redirect.target for redirect in command.redirects if redirect.target is not None)
                # Pushed first, popped last: executed bodies claim shared lists.
                for word in words:
                    stack.extend((nested, True) for nested in word.nested)
                if command.subshell is not None:
                    stack.append((command.subshell, quoted))
                for word in words:
                    stack.extend((sub.body, quoted) for sub in word.subs if sub.body is not None)
    return found
