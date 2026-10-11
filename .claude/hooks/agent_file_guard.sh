#!/usr/bin/env bash
# PreToolUse entry point for agent_file_guard.py (see that file).
#
# Runs the guard with the first interpreter that can: the project venv
# (Windows or POSIX layout), then python3/python on PATH; the guard needs
# only the standard library. Exit 0 allows, 2 blocks with the guard's
# reason on stderr. Any other result (interpreter missing or broken, guard
# file missing) moves on to the next interpreter.
#
# If no interpreter can run the guard, only calls whose hook input carries
# one of the four guarded project agent types are blocked. Every other call,
# the main session's included, is allowed, so a missing or rebuilt venv
# cannot lock the main session out.
#
# stdin is read once with cat: bash's read builtin reads a pipe byte by byte
# (seconds for a large Write/Edit input), and /dev/stdin cannot be opened
# for the native pipe Claude Code passes on Windows.

if command -v cat >/dev/null 2>&1; then
    input=$(cat)
else
    IFS= read -r -d '' input
fi
guard="$CLAUDE_PROJECT_DIR/.claude/hooks/agent_file_guard.py"

if [[ -f $guard ]]; then
    for py in "$CLAUDE_PROJECT_DIR/.venv/Scripts/python.exe" \
              "$CLAUDE_PROJECT_DIR/.venv/bin/python" python3 python; do
        command -v "$py" >/dev/null 2>&1 || continue
        err=$(printf '%s' "$input" | "$py" "$guard" 2>&1 >/dev/null)
        rc=$?
        if [[ $rc -eq 0 ]]; then
            exit 0
        fi
        if [[ $rc -eq 2 && $err == "Blocked by Apply101 agent_file_guard"* ]]; then
            printf '%s\n' "$err" >&2
            exit 2
        fi
    done
fi

guarded='"agent_type"[[:space:]]*:[[:space:]]*"project-(auditor|developer|reviewer|supervisor)"'
if [[ $input =~ $guarded ]]; then
    echo "Blocked by Apply101 agent_file_guard: no Python could run agent_file_guard.py, so project agents are blocked until it can (the main session is not affected)." >&2
    exit 2
fi
exit 0
