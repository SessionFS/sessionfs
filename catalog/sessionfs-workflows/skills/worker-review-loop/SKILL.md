---
name: worker-review-loop
description: Build a change with brief-driven coding agents in isolated git worktrees, gated by an independent reviewer whose verdict is bound to the exact commit, with token and round budgets. Uses SessionFS review loops (start_review_loop, start_worker_run, finish_worker_run, record_review_round, check_merge_readiness), the project's learned checklist, and loop reports. Use when the user wants to split work across worker agents, run a worker/reviewer loop, or gate a merge on an independent review.
---

# Worker review loop

You coordinate. Workers write code, one branch each, from a written brief.
A separate reviewer reads the code and records a verdict bound to the commit
it reviewed. Nothing merges until that verdict is clean for the exact head
commit. SessionFS records every run, round, finding and token, so anyone can
see where each piece of work stands and what the loop caught.

Your own agent orchestrates (any tool with MCP or a shell). SessionFS
records, gates and learns; it never runs a model for you.

| Step | One call |
| --- | --- |
| Start a loop | `start_review_loop` (`sfs loop start`) |
| Worker starts / finishes | `start_worker_run` / `finish_worker_run` (`sfs loop run start|finish`) |
| Review round | `record_review_round` (`sfs loop review`) |
| Is it safe to merge? | `check_merge_readiness` (`sfs loop ready`) |
| Where does it stand? | `get_loop_status` (`sfs loop status`) |
| Raise a limit | `update_review_loop` (`sfs loop update`) |
| What did it catch, what did it cost? | `get_loop_report` (`sfs loop report`, `sfs loop insights`) |
| Traps to check every time | `get_loop_checklist`, `propose_checklist_item` (`sfs loop checklist`) |

## 0. Before any code: design review

One reviewer reads the spec (not code) and answers four questions in
writing:

1. What happens to access when a step is slow (an LLM call, an external
   API, an upload)? Who could lose or gain access while it runs?
2. Where does money or quota get spent, and is it counted on every path,
   including errors and cancellation?
3. What runs in parallel or twice (double clicks, retries, two workers),
   and what guarantees exactly-once?
4. Which values come from users or models and end up as identities,
   permissions or stored data?

Record it as round 0 against the loop's parent issue once the loop exists:
`record_review_round` with `stage: "design"`. Fix the spec before workers
start. A design flaw found here costs one edit; found in code review it
costs a fix pass and another round.

## 1. Start the loop

Once per project, an org admin registers the reviewer's identity, ideally a
service key used by nothing else and minted by a different person than the
implementer's key:

```sh
sfs admin trusted-reviewers add --org <org_id> --service-key-id <key_id> --persona <reviewer-persona>
```

A key counts as the person who minted it: two keys minted by the same person
are one identity, and that person cannot review their own work.

Then one call starts the loop:

```text
start_review_loop(
  title: "<feature>",
  tickets: [{title, description, acceptance_criteria: [...]}, ...],
  reviewer_persona: "<reviewer-persona>",
  token_budget: 2000000,         # optional
  max_review_rounds: 3,          # optional, no default
  cost_budget_usd: 25            # optional, org owners/admins only
)
```

Pass `from_ticket_id` to adopt an existing issue as the loop instead of
creating one. The result has the `loop_id`, one child ticket per brief with
its rendered `brief_markdown` (the description, the acceptance criteria and
a **Known traps** section from the project's active checklist), the
`queue_id`, whether the reviewer is trusted (with a setup hint if not), and
the environment for each role (`SESSIONFS_PROFILE=implementer` /
`reviewer`). Run each role under its own profile, never the reviewer under
a worker's identity.

Write each brief so it is the worker's only context:

```markdown
Goal: <one sentence: what changes and why>
Spec: <path to the spec; quote the parts that matter>
Read first: <3-8 files, in order>
Scope: change only <paths>. Do not touch <paths>.
External APIs: <vendor docs URL per API; request shapes must match them>
Required tests: <test files>, <command that must pass>,
  <migration proof on a real database, if the change has migrations>
Done means: every acceptance criterion holds, tests pass, work is
  committed on the branch, and your final message contains
  WORKER-STATUS: DONE
```

## 2. Build: one worker, one worktree, one run

```sh
git worktree add ../wt-<branch> -b <branch> <base-branch>
```

Never point two workers at the same worktree or branch.

1. `start_worker_run(ticket_id, model, provider, branch)` takes the ticket
   lease and returns a `run_id` and what is left of the budget. It refuses
   with `loop_needs_human` when the ticket is parked because a budget or
   round cap was hit.
2. Launch your coding agent in the worktree with the brief.
3. When it exits, confirm with git, not the worker's summary (search the
   whole log; tools wrap and prefix their last line):

   ```sh
   grep -q 'WORKER-STATUS: DONE' worker.log \
     && [ -z "$(git -C ../wt-<branch> status --porcelain)" ] \
     && [ "$(git -C ../wt-<branch> rev-list --count <base-branch>..HEAD)" -gt 0 ] \
     && echo finished
   ```

4. `finish_worker_run(run_id, head_sha, exit_reason: "finished",
   input_tokens, output_tokens, cost_usd)` with the commit the worker left
   and the tokens it used. Calling it twice with the same `head_sha` is
   safe and never counts usage twice.

Out of turns: commit what exists, finish the run with
`exit_reason: "out_of_turns"` (a crash is `"crashed"`), then start a new run on the same ticket with a
short continuation brief ("the previous worker stopped at <sha>; read its
diff first; remaining: <unmet criteria>"). Crashed: fix the cause and
relaunch the same brief in the same worktree.

## 3. Review: read-only, code not conclusions

The reviewer runs in its own checkout, under the reviewer profile, and
never commits. Round 1 prompt, with the worker's summary left out:

```markdown
Review branch <branch> at commit <sha> against base <base>, for ticket
<ticket_id>. Read the acceptance criteria and the diff. Do not trust
comments or summaries from the author; check the code. Run <test command>.
Check every external request against the vendor's documentation, not the
mocks. Go through the brief's Known traps item by item. You may not edit
files. For every finding give: severity, class (a checklist slug, or
"new"), and harm: data-exposed, money-spent, wrong-actor, crash, other, or
none.
```

Rounds after the first are narrow: "verify each fix, then review only the
diff since <sha>". Ask for a full re-review only when a fix touched access
checks, locking or money.

Record each round in one call:

```text
record_review_round(
  loop_id, ticket_id, round: 1, stage: "code",
  reviewed_commit: "<full sha>",
  verdict: "CHANGES-REQUESTED" | "VERIFIED-CLEAN",
  findings: [{severity, class, harm, file, line, title}],
  model, provider, input_tokens, output_tokens, cost_usd
)
```

The server decides whether the verdict is trusted from the credential that
sent it, never from the request. Repeating a round is safe. A finding with
class `new` proposes a checklist item, so the next brief warns about it.
Going past the round cap or the token budget parks the ticket for a person.

## 4. Fix and re-review

Start a fix worker with `start_worker_run` in the same worktree, with a
brief listing the open findings that have real harm. Finish it with the new
`head_sha`, then the same reviewer runs the next round against that commit.

Harm decides whether there is another round. A finding with real harm is
fixed and re-checked. Findings with harm `none` go into one follow-up
ticket and do not start a round. Fix one-line or wording findings yourself
and verify them with a targeted test. After three rounds on one branch,
stop and look at why findings keep coming (the brief, the design, or the
model) before paying for a fourth.

## 5. Merge

```text
check_merge_readiness(ticket_id, sha: "<head commit you are about to merge>")
```

It is `ready` only when a trusted VERIFIED-CLEAN verdict is bound to exactly
that commit, the reviewer is a different identity from every implementer,
and no harmful finding is open. Any new commit needs a new clean round. With
the GitHub App installed, the same answer is a "SessionFS review loop" check
on the pull request that branch protection can require. A person merges.

## 6. When something needs you

`get_loop_status` (or the dashboard's Review Loop tab and Inbox) shows what
is ready to merge, what hit a limit, and which checklist items were
proposed. Raise a limit with `update_review_loop`; close a parked item with
`sfs loop close-item` (people only, never a service key); accept or retire
checklist items with `sfs loop checklist accept|retire`.

## 7. Spend tokens where they catch bugs

- One worker, one brief, one context window. A brief that needs more than
  roughly 150 turns is two briefs. Split by phase, and put new code in new
  files so phases run in parallel worktrees without conflicts.
- Tell workers to commit after each step that works; running out of turns
  then costs a short continuation, not a rebuild.
- Routine build work goes to the cheapest model that passes review; use a
  stronger model for UI, design, reviewers and security-sensitive work.
  Check the provider's balance before every launch.
- Point workers at paths and line ranges; never paste files into a brief.
- Reuse the same reviewer across rounds and keep re-review prompts narrow.
  A second reviewer gets one final pass over the changed code only.
- Workers run targeted tests per step; you run the full suite once on the
  merged result.

Report usage on every run (`finish_worker_run`, `record_review_round`, or
`report_agent_run_usage` for long runs). `get_loop_report` and
`sfs loop insights` then show what the loop caught by harm, cost per
accepted change, how often each checklist class still turns up, and which
worker/reviewer model pairings do best.

## 8. Ship

Record the release with `sfs release record <version> --tag-sha <sha> --pr
<number>` (or let the GitHub/GitLab webhooks do it), then
`sfs release ship-report <version> --markdown` gives a factual summary for
the release notes: rounds, findings, harmful findings fixed before merge,
cost per accepted change, checklist classes learned.

## Rules

- The worker's self-report is never the gate. The commit-bound trusted
  verdict is.
- A passing mocked test can encode the wrong external API. Check request
  shapes against vendor documentation and prove migrations on a real
  database.
- Record what you learn with `add_knowledge` as you learn it.
- Never put secrets, keys or credentials in a brief, a finding or a log you
  share.
