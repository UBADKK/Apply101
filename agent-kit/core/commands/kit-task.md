---
description: Run one bounded task through the agent kit (researcher, auditor, developer, reviewer) with this session as orchestrator
argument-hint: <task goal>
---

Act as the orchestrator defined in `.claude/agent-kit/ORCHESTRATION.md`.
Before anything else, read that file, `.claude/agent-kit/SAFETY_POLICY.md`
and `.claude/agent-kit/PROJECT_CONTEXT.md`, and follow them exactly:

- create the checkpoint first with `python3 .claude/agent-kit/kit.py checkpoint new`;
- delegate only to `kit-researcher`, `kit-auditor`, `kit-developer` and
  `kit-reviewer`, with self-contained prompts;
- record every agent result with `checkpoint event` and respect the gates
  and loop limits it enforces;
- stop and ask me whenever the contract says so; never commit, push, merge
  or deploy unless I ask for exactly that.

Task: $ARGUMENTS
