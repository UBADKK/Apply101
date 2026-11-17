#!/usr/bin/env bash
# PreToolUse entry point for kit_guard.py (see that file).
#
# Runs the guard with the first interpreter that can: a project venv
# (Windows or POSIX layout), then python3/python on PATH; the guard needs
# only the standard library. Exit 0 allows, 2 blocks with the guard's
# reason on stderr. Any other result (interpreter missing or broken, guard
# file missing) moves on to the next interpreter.
#
# If no interpreter can run the guard, only calls whose hook input carries
# one of the four kit agent types are blocked. Every other call, the main
# session's included, is allowed, so a missing Python cannot lock the main
# session out.
#
# stdin is read once with cat: bash's read builtin reads a pipe byte by byte.

if command -v cat >/dev/null 2>&1; then
    input=$(cat)
else
    IFS= read -r -d '' input
fi
guard="$(dirname "${BASH_SOURCE[0]}")/kit_guard.py"

if [[ -f $guard ]]; then
    for py in "$CLAUDE_PROJECT_DIR/.venv/Scripts/python.exe" \
              "$CLAUDE_PROJECT_DIR/.venv/bin/python" python3 python; do
        command -v "$py" >/dev/null 2>&1 || continue
        err=$(printf '%s' "$input" | "$py" "$guard" 2>&1 >/dev/null)
        rc=$?
        if [[ $rc -eq 0 ]]; then
            exit 0
        fi
        if [[ $rc -eq 2 && $err == "Blocked by agent-kit guard"* ]]; then
            printf '%s\n' "$err" >&2
            exit 2
        fi
    done
fi

kit_agent='"agent_type"[[:space:]]*:[[:space:]]*"kit-(researcher|auditor|developer|reviewer)"'
if [[ $input =~ $kit_agent ]]; then
    echo "Blocked by agent-kit guard: no Python could run kit_guard.py, so kit agents are blocked until it can (the main session is not affected)." >&2
    exit 2
fi
exit 0
