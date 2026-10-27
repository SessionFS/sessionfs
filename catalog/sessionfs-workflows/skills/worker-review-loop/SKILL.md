---
name: worker-review-loop
description: Build a change with brief-driven coding agents in isolated git worktrees, gated by an independent read-only reviewer, a fix pass, and a re-review by the same reviewer before merge. Uses SessionFS tickets, agent runs, a review_until_clean work queue with a trusted reviewer, handoffs, and the knowledge base. Use when the user wants to split work across worker agents, run a worker/reviewer loop, or gate a merge on an independent review.
---

# Worker review loop

You coordinate. Workers write code, one branch each, from a written brief. A
separate reviewer reads the code and posts a verdict. Nothing merges until
the same reviewer says the fix pass is clean. SessionFS holds the state, so
any agent or teammate can see where each piece of work stands.

| Piece of the loop | SessionFS primitive |
| --- | --- |
| Brief | A ticket with acceptance criteria |
| Each worker or reviewer launch | An agent run |
| Review loop | A work queue in `review_until_clean` mode |
| Reviewer | A registered trusted reviewer posting verdicts |
| Worker out of turns | A continuation run or a handoff |
| Discoveries | `add_knowledge` entries |

## 1. Write the brief as a ticket

The brief is the worker's only context. If it is not in the ticket, the
worker does not know it. Create one ticket per branch with `create_ticket`
(or `sfs ticket create`), using this shape:

```markdown
Goal: <one sentence: what changes and why>
Spec: <link or path to the spec; quote the parts that matter>
Read first: <3-8 files, in order>
Scope: change only <paths>. Do not touch <paths>. No new dependencies
  unless listed here.
External APIs: <vendor docs URL per API; request shapes must match them>
Required tests: <test files to add or extend>, <command that must pass>,
  <migration proof on a real database, if the change has migrations>
Done means: every acceptance criterion holds, tests pass, work is
  committed on the branch, and your final message contains
  WORKER-STATUS: DONE
```

Put each testable outcome in `acceptance_criteria`, not in prose. Agent-made
tickets start as `suggested`; a person approves them (`approve_ticket` or
`sfs ticket approve`) before a queue will pick them up.

## 2. Isolate each worker

One worker per branch, each in its own worktree, so workers never see or
overwrite each other's files:

```sh
git worktree add ../wt-<branch> -b <branch> <base-branch>
```

Never point two workers at the same worktree or branch.

## 3. Launch the worker

1. Record the run: `create_agent_run` with `persona_name`, `ticket_id` and
   `start_now: true`, or `sfs agent run <persona> --ticket <ticket_id>
   --output-id`. The persona must exist (`create_persona` or
   `sfs persona create`).
2. Start your coding agent (any tool) inside the worktree with the brief.
   Tell it to call `start_ticket` first and to record durable findings with
   `add_knowledge` as it goes.
3. Write the agent's full output to a log file you keep.

Cheap fast models often do well as workers when a stronger model reviews,
but that split is optional.

## 4. Detect how the worker exited

Do not trust the worker's own summary, and do not anchor the "finished"
check to the start of a line: tools wrap, indent or prefix their final
message, and an anchored check has missed real out-of-turns exits. Search
the whole log, then confirm with git:

```sh
log=worker.log; wt=../wt-<branch>
done_line=$(grep -c 'WORKER-STATUS: DONE' "$log")   # anywhere in a line
clean=$(git -C "$wt" status --porcelain | wc -l)
commits=$(git -C "$wt" rev-list --count <base-branch>..HEAD)
if [ "$done_line" -gt 0 ] && [ "$clean" -eq 0 ] && [ "$commits" -gt 0 ]; then
  echo finished
elif grep -qiE 'max[ _-]?turns|turn limit' "$log"; then
  echo out-of-turns
else
  echo crashed
fi
```

Adapt the out-of-turns pattern to the message or exit status your tool
uses. Log only the agent's output: if the tool echoes the prompt, the
brief's own status line will match.

| Exit | Signs | Do this |
| --- | --- | --- |
| Finished | Status line present, tree clean, commits on the branch | `complete_agent_run` with `status: passed`. Call `complete_ticket` (`notes` plus `changed_files`) so the ticket moves `in_progress` → `review` (first pass only: fix-pass workers skip it, the ticket is already in review), then post one ticket comment: branch, commit, test command. Hand it to review (step 5). |
| Out of turns | Turn-limit message, no status line | Commit what exists on the branch as work in progress. `complete_agent_run` with `status: errored`. Start a continuation (below). |
| Crashed | Non-zero exit, no turn-limit message | `complete_agent_run` with `status: errored` and the error. Fix the cause, then relaunch the same brief in the same worktree. |

**Continuation.** Write a short continuation brief: "Continue ticket
<ticket_id>. The spec is the ticket. The previous worker stopped at commit
<sha> on <branch>; read its diff against the base first. Remaining: <the
unmet criteria>." Create the run with `replay_agent_run` (or
`sfs agent replay <run_id> --prompt-file <file> --context-ref <sha>`) so it
links to the run that stopped, then start it with
`sfs agent run --run-id <new_run_id>` and launch the worker on the
continuation brief in the same worktree. The ticket is already
`in_progress`, so the continuation worker skips `start_ticket`. If a person should continue
instead, push the session and hand it off with the session-handoff skill,
passing the ticket id.

## 5. Set up the review loop

Once per project, an org admin registers the reviewer's identity, ideally a
service key used by nothing else:

```sh
sfs admin trusted-reviewers add --org <org_id> --service-key-id <key_id> --persona <reviewer-persona>
```

Run the reviewer under that identity only, never under a worker's: for
example a separate auth profile (`sfs auth login --profile <name> --key
<key>`, then `SESSIONFS_PROFILE=<name>` in the reviewer's shell).

Create the queue with `create_work_queue`: `mode: review_until_clean`,
`selector: {"ticket_ids": [...]}`, and `assigned_persona` equal to the
registered persona. The server only trusts a verdict when the reviewer's
identity is registered for exactly that persona.

## 6. Review: read-only, code not conclusions

The reviewer runs in its own checkout, read-only, and never commits. Each
wake it calls `run_work_queue_step`. A `post_review` directive means a
worker comment arrived; the directive carries `review_round` and the open
findings from earlier rounds. Wake no faster than the queue cadence (at
least 120 seconds).

Give the reviewer this prompt, with the worker's summary left out:

```markdown
Review branch <branch> at commit <sha> against base <base>, for ticket
<ticket_id>. Read the ticket's acceptance criteria and the diff. Do not
trust comments or summaries from the author; check the code. Run
<test command>. Check every external request against the vendor's
documentation, not against the mocks in the tests. Confirm migrations were
run on a real database. Re-check each open finding from earlier rounds.
You may not edit files.
```

The reviewer records its own agent run (`create_agent_run`, then
`complete_agent_run` with `severity` and `findings`), and delivers the
verdict through `complete_work_queue_step` with the directive's ids,
`outcome: posted_review`, and `verdict_content`. Post the verdict through
the queue, not `add_ticket_comment`, so the server writes it with the
queue's persona and the round the directive tracks. The server parses this
exact format, with the reviewer's own name in place of `<Reviewer>`
whichever tool reviews:

```text
<Reviewer> R<review_round> review on <ticket_id>: CHANGES_REQUESTED
Findings:
- HIGH — <file>:<line> <what is wrong and why>
- LOW — <...>
```

`<Reviewer>` must start with a letter and use only letters, digits, `.`,
`_` or `-`, at most 32 characters — a `verdict_content` whose header does
not parse is rejected with `verdict_header_unparseable` before anything is
written.

## 7. Fix pass and re-review

1. Read the findings with `get_ticket_review_state` (or
   `sfs ticket review-state <ticket_id>`).
2. Launch a fix worker in the same worktree with a brief that lists the open
   findings and points at the ticket. Same exit handling as step 4, except
   that the ticket is already in `review`: the fix worker calls neither
   `start_ticket` nor `complete_ticket`. It works the brief and commits.
3. The fix worker commits and posts one ticket comment with the new commit.
   Any comment that is not a trusted verdict hands the turn back to the
   reviewer, so post one comment per pass, not running chatter.
4. The same reviewer re-reviews. The loop ends only on a strict
   `<Reviewer> R<N> review on <ticket_id>: VERIFIED-CLEAN` with no open
   findings. Close rounds with `Findings: none.`

## 8. Merge

Merge only when `get_ticket_review_state` shows
`last_verdict_is_strict_verified_clean: true` and no open findings
(`sfs ticket review-state` prints `Strict VERIFIED-CLEAN: yes`). Merge
the branch, run the full test suite on the merged result, then
`resolve_ticket`. The queue never changes ticket status itself. Remove the
worktree with `git worktree remove`.

## Rules

- The worker's self-report is never the gate. The trusted verdict is.
- A passing mocked test can encode the wrong external API. Check request
  shapes against the vendor's documentation, and prove database migrations
  on a real database.
- Record what you learn (API quirks, failed approaches, conventions) with
  `add_knowledge` as you learn it.
- Never put secrets, keys or credentials in a brief, a comment or a log you
  share.
