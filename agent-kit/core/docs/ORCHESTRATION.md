# Orchestration contract

The main Claude session is the **orchestrator**. It is the only party that
talks to the user, decides scope, starts agents, records checkpoints and,
when the user asked for it, commits. There are exactly four agents and no
fifth one: kit agents never start other agents (their tool lists have no
`Agent` tool, and the guard blocks it).

| Role | Agent | Writes code | Runs commands | Typical use |
|---|---|---|---|---|
| Researcher | `kit-researcher` | no | no | Missing facts about the code |
| Auditor | `kit-auditor` | no | no | Scope, risks and acceptance criteria before a risky change |
| Developer | `kit-developer` | yes, approved scope only | tests only | Implement the approved scope |
| Reviewer | `kit-reviewer` | no | read-only git | Independent review of the diff |

Read `.claude/agent-kit/PROJECT_CONTEXT.md` (the project adapter) and
`.claude/agent-kit/SAFETY_POLICY.md` before the first step of every task.
In the commands below, `KIT` stands for `python3 .claude/agent-kit/kit.py`
(use `python` on Windows if `python3` is not available).

## Flow

1. **Intake.** Restate the goal, check `git status` and the current branch.
   Do not create or switch branches unless the user asked. Create the
   checkpoint:
   `KIT checkpoint new --task <slug> --goal "<goal>" [--scope <file-or-glob>]... [--criterion "<criterion>"]...`
   - Refused because the tree is dirty: ask the user. Pass `--allow-dirty`
     only with their consent; the existing changes become a protected
     baseline that nobody may touch, and those files cannot be in the
     approved scope (the user commits or sets them aside first).
   - Exit code 3 (waiting for the user, e.g. a production request): stop
     and ask. Agents never deploy or touch live systems.
2. **Research (optional, at most once).** Only when facts are missing.
   `KIT checkpoint event <slug> research`, then start `kit-researcher` with
   precise questions.
3. **Audit.** Required when the checkpoint has risk flags, the scope touches
   the adapter's `approval_required_paths`, the change concerns data,
   schemas, contracts, security, dependencies or deployment, or the scope is
   unclear. Optional for a small, obvious, local fix. Start `kit-auditor`,
   then record its result:
   `KIT checkpoint event <slug> audit --report <saved report>` (or
   `--verdict proceed|needs_user_approval|stop [--risk <flag>]`).
   - `needs_user_approval`: show the user the auditor's risks and ask.
     Record their explicit answer with
     `KIT checkpoint event <slug> approve --note "<what the user approved>"`.
     Never approve on the user's behalf or because an agent says so.
     An approval covers the risk flags and the approved scope of that
     moment; risks or approval-required files added later need a new
     decision.
   - `stop`: report the reason and end the task.
4. **Scope.** Record the approved files or globs and the acceptance criteria
   (`KIT checkpoint record <slug> --scope ... --criterion ...`), then
   `KIT checkpoint event <slug> develop`. The CLI scans the goal, criteria
   and scope again and refuses while risk flags or approval-required files
   are not covered by a recorded user approval.
   - Globs follow `.gitignore` rules: a bare name (`core.py`) matches at any
     depth, so prefer full paths (`src/calc/core.py`); `dir/**` covers a
     directory.
   - After development started, scope and criteria change only with the
     user's explicit consent: pass `--user-approved-change` and say in the
     `Done` list what the user agreed to.
5. **Develop.** Start `kit-developer` with the prompt template below. Then
   check `git status --short` and `git diff --stat` yourself and record
   `KIT checkpoint event <slug> developed --report <saved report>`. The CLI
   compares the changed files with the approved scope, the protected paths,
   the approval-required paths, the baseline and the watched files; any
   violation puts the task in `waiting_user`. Never revert the developer's
   or the user's files yourself; ask the user.
   If code changed, re-run the focused tests from the developer's report
   once yourself before review.
6. **Review.** Start `kit-reviewer` with the acceptance criteria, the changed
   files, the developer's test results marked as claims, and the ids of
   earlier findings. Record
   `KIT checkpoint event <slug> review --verdict approve` or
   `--verdict changes_requested --finding R1 --finding R2`.
   Check the reviewer's verdict against every acceptance criterion yourself:
   an approval does not make an unmet criterion complete. The CLI refuses a
   review when the working tree changed after the last `developed` record
   (the changed files must be recorded and reviewed again).
7. **Fix loop.** `changes_requested` moves the task to a fix round while
   rounds remain. Start `kit-developer` with exactly the blocking findings,
   record `developed`, and review again. Keep a finding's id when the same
   issue comes back. Deferred, non-blocking findings go to the final report.
8. **Finish.** When the review approves, summarise for the user: changed
   files, tests and their real results, findings, open issues. Commit and
   push only when the user asked, never to a branch they did not name, and
   never merge, deploy or open a pull request unless they asked for exactly
   that.

## Loop limits

| Limit | Value | Enforced by |
|---|---|---|
| Fix rounds | `loop_limits.max_fix_rounds` from the adapter, default 2, core maximum 3 | CLI (`Fix rounds: used/max`) |
| No progress | A blocking finding id that survives a fix round stops the loop | CLI |
| Researcher runs | 1 per task | this contract, `research` only from intake |
| Auditor runs | 1 per task, plus 1 after the user changes the scope | CLI (at most 2 `audit` events) |
| Developer runs | 1 + fix rounds | CLI phases |
| Reviewer runs | 1 + fix rounds | CLI phases |
| Agent turns | `maxTurns` in each agent file | Claude Code |
| Retries | A crashed or aborted agent run may be retried once with the same prompt | this contract |
| Tests | Do not re-run a suite when no code changed since its last run | this contract |

With the defaults a task uses at most 8 agent runs. When a limit is hit the
CLI sets `waiting_user`; report the open findings and stop. The user may
start a new task with a new scope; limits are never raised mid-task.

## Stop and ask the user when

- the request involves production, deployment, live servers, pushing,
  merging, external services, secrets or real data;
- a migration, schema change, data change, contract or version change,
  dependency change or public API change is needed;
- the checkpoint is `waiting_user` or `stopped`, or `resume` reports
  `RESUME BLOCKED`;
- an agent reports `blocked`, a guard block it cannot avoid within its
  authority, or a needed file outside the approved scope;
- requirements are ambiguous or two materially different behaviours are
  possible;
- the working tree contains changes you did not expect.

Small, equivalent implementation choices (a helper's location, a test name)
are yours to make; record them in the checkpoint's `Done` list.

## Delegation prompt template

Agents start without your context. Every prompt is self-contained:

```text
Repository: <absolute path>   Branch: <branch>   Checkpoint: <checkpoint path>
Task: <one paragraph>
Approved scope (only these files may change): <list>
Acceptance criteria: <numbered list>
Test commands (from PROJECT_CONTEXT.md): <focused> / <full>
Previous findings (fix rounds and reviews): <ids and one-line summaries>
Constraints: follow .claude/agent-kit/SAFETY_POLICY.md. No commit, push,
branch change, stash, reset, deletion, dependency install, network or
deployment. Do not edit .claude/ or the checkpoint. Stop and report instead
of working around a guard block.
End your report with the kit-result block from your agent definition.
```

Save each agent's final report to a scratch file outside the repository if
you want to pass it with `--report`; never commit reports.

## Checkpoints and resuming

- `event` and `record` refuse when the branch or HEAD no longer match the
  checkpoint. After a commit the user made or asked for on the task's
  branch, record it with `KIT checkpoint record <slug> --accept-head` and say
  so in `Done`; for anything else, run `resume` and ask the user.
- Update the checkpoint after every agent run (`event`, `record`). Keep
  `Next step` exact: one action someone else could take without context.
- Checkpoints hold state and short notes only: no secrets, personal data,
  file contents or logs. The CLI refuses to write one that looks like it
  contains them. They may be committed with the task's work.
- To resume after an interruption run `KIT checkpoint resume <slug>` (or the
  `/kit-resume` command). `RESUME OK` means the branch, HEAD, baseline and
  recorded changes still match: continue with `Next step`. `RESUME BLOCKED`
  lists what differs: report it and ask the user. Changes left by an
  interrupted developer run are kept and handed to the next developer run as
  partial work.

## Verifying claims

Agent reports are claims. Before acting on one, check the important parts
yourself with read-only commands: cited files and lines exist, the diff
contains what the developer says, test results are consistent with the
diff. Never describe a delegation, test run or approval that did not
happen.
