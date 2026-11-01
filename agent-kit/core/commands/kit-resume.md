---
description: Resume an interrupted agent-kit task from its checkpoint
argument-hint: <task slug>
---

Act as the orchestrator defined in `.claude/agent-kit/ORCHESTRATION.md`.
Read it, `.claude/agent-kit/SAFETY_POLICY.md` and
`.claude/agent-kit/PROJECT_CONTEXT.md` first.

Then run `python3 .claude/agent-kit/kit.py checkpoint resume $ARGUMENTS` and
read the checkpoint it names.

- `RESUME OK`: continue with the checkpoint's `Next step`, within the
  recorded scope and loop limits. Keep any changes an interrupted developer
  run left behind and hand them to the next developer run as partial work.
- `RESUME BLOCKED`: do not continue. Report what differs and ask me how to
  proceed. Do not switch branches, revert files or edit the checkpoint by
  hand to make it pass.
