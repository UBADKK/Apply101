# Checkpoint template

A checkpoint lets a new session continue a task after a session limit or an
interruption. Create and update it with `kit.py checkpoint` (`new`,
`record`, `event`); the CLI keeps the format, checks the workflow gates and
refuses content that looks like secrets, personal data or logs. Files live
in the adapter's `checkpoint_dir` (default `.claude/agent-kit/checkpoints`),
one per task, and are small enough to commit with the task's work.

Rules:

- Short notes only: no secrets, e-mail addresses, file contents, stack
  traces or long logs (at most 12 KB, 220 lines, 400 characters per line).
- `Next step` is one exact action that someone without context can take.
- `Baseline` stores sizes and modification times, never contents.
- Sections stay in this order; an empty section holds `-`.

```markdown
# Checkpoint: <task-slug>

<!-- kit-checkpoint v1: keep it small; no secrets, personal data, file contents or logs. -->

## State
- Task: <task-slug>
- Status: active | waiting_user | done | stopped
- Phase: intake | research | audit | develop | review | fix | done
- Reason: <why the task waits or stopped, else ->
- Branch: <branch>
- Base commit: <commit when the task started>
- Head commit: <commit at the last update>
- Fix rounds: <used>/<max>
- Risk flags: <production, remote_git, data, secrets, external, approval_path, auditor, or ->
- Approval: <the user's recorded decision, else ->
- Updated: <UTC timestamp>

## Goal
<what the user asked for, in one or two sentences>

## Approved scope
- <file or glob the developer may change>

## Acceptance criteria
- <observable criterion>

## Done
- <completed step or small decision and its reason>

## Changed files
- <path, filled in by the `developed` event>

## Tests run
- `<command>` -> <short result, e.g. OK (12 tests)>

## Open issues
- <finding id>: <one line>

## Next step
<one exact action>

## Baseline
- watch <path> size=<bytes> mtime_ns=<ns>
- dirty <path> size=<bytes> mtime_ns=<ns>

## Log
- <event>: <result>
```
