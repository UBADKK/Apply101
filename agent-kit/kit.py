#!/usr/bin/env python3
"""Developer Agent Kit command line (standard library only).

From the kit source tree:
    python3 kit.py check                         validator + all kit tests
    python3 kit.py install --target <project> [--context <adapter.md>]
    python3 kit.py validate --project <project>

From an installed project (python3 .claude/agent-kit/kit.py ...):
    validate | check
    checkpoint new --task <slug> --goal <text> [--scope <glob>]... [--criterion <text>]...
    checkpoint record <task> [--done/--test/--issue/--scope/--criterion <text>] [--next <text>]
    checkpoint event <task> <research|audit|approve|develop|developed|review|stop>
                     [--verdict <v> | --report <file>] [--finding <id>]... [--risk <flag>]... [--note <text>]
    checkpoint verify <task> | checkpoint resume <task> | checkpoint show <task>

Exit codes: 0 ok, 1 usage or internal error, 2 validation failed,
3 refused or waiting for the user.
"""

import argparse
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kitlib import checkpoint as cpmod  # noqa: E402
from kitlib import workflow  # noqa: E402
from kitlib.common import KitError, KitRefused, Layout, load_context, read_text  # noqa: E402
from kitlib.install import install  # noqa: E402
from kitlib.validate import run as run_validate  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_INVALID, EXIT_REFUSED = 0, 1, 2, 3


def _project(args, layout):
    if getattr(args, "project", None):
        return Path(args.project).resolve()
    if layout.project_root is not None:
        return layout.project_root
    raise KitError("pass --project <path to the project root>")


def cmd_validate(args, layout):
    project = Path(args.project).resolve() if args.project else None
    report = run_validate(layout, project)
    print("\n".join(report.lines()))
    return EXIT_OK if report.ok else EXIT_INVALID


def cmd_check(args, layout):
    status = cmd_validate(args, layout)
    if not layout.is_source or args.project:
        return status
    suites = unittest.TestSuite()
    loader = unittest.TestLoader()
    test_dirs = [layout.kit_root / "tests"] + sorted(
        p for p in (layout.kit_root / "adapters").glob("*/") if p.is_dir()
    )
    for directory in test_dirs:
        if any(directory.glob("test_*.py")):
            suites.addTests(loader.discover(str(directory), pattern="test_*.py", top_level_dir=str(directory)))
    print(f"\nRunning kit tests ({suites.countTestCases()} tests) ...")
    result = unittest.TextTestRunner(verbosity=1 if not args.verbose else 2).run(suites)
    ok = status == EXIT_OK and result.wasSuccessful()
    print("CHECK PASSED" if ok else "CHECK FAILED")
    return EXIT_OK if ok else EXIT_INVALID


def cmd_install(args, layout):
    for line in install(args.target, args.context, args.force, args.dry_run, layout):
        print(line)
    if not args.dry_run:
        print("\nNext: fill .claude/agent-kit/PROJECT_CONTEXT.md, re-run install if you added "
              "protected_paths, then run: python3 .claude/agent-kit/kit.py validate")
    return EXIT_OK


def _load_cp(path):
    if not path.is_file():
        raise KitError(f"checkpoint not found: {path}")
    text = read_text(path)
    try:
        return cpmod.parse(text)
    except cpmod.CheckpointError as exc:
        raise KitError(f"{path.name}: {exc}")


def _save_cp(path, cp, project):
    workflow.touch(project, cp)
    text = cpmod.render(cp)
    problems = cpmod.verify(text)
    if problems:
        raise KitError("checkpoint would be invalid, nothing written: " + "; ".join(problems))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def cmd_checkpoint(args, layout):
    project = _project(args, layout)
    _text, config = load_context(project)
    if args.action == "new":
        path = workflow.checkpoint_path(project, config, args.task, slug_only=True)
        if path.exists():
            raise KitRefused(f"checkpoint already exists: {path.relative_to(project)}; use resume")
        try:
            cp = workflow.new_checkpoint(project, config, args.task, args.goal, args.scope or [],
                                         args.criterion or [], args.allow_dirty)
        except KitError as exc:
            print(f"kit: refused: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        _save_cp(path, cp, project)
        print(f"created {path.relative_to(project)}  status={cp['state']['Status']}  "
              f"risk={cp['state']['Risk flags']}")
        print(f"Next step: {cp['Next step']}")
        return EXIT_REFUSED if cp["state"]["Status"] == "waiting_user" else EXIT_OK

    path = workflow.checkpoint_path(project, config, args.task)
    if args.action == "verify":
        problems = cpmod.verify(read_text(path)) if path.is_file() else [f"not found: {path}"]
        print("\n".join(problems) if problems else "CHECKPOINT OK")
        return EXIT_INVALID if problems else EXIT_OK
    cp = _load_cp(path)
    if args.action == "show":
        print(cpmod.render(cp), end="")
        return EXIT_OK
    if args.action == "resume":
        ok, lines = workflow.resume_report(project, config, cp)
        print("\n".join(lines))
        return EXIT_OK if ok else EXIT_REFUSED
    if args.action in ("record", "event") and not getattr(args, "accept_head", False):
        drift = workflow.repository_drift(project, cp)
        if drift:
            print("kit: refused: " + "; ".join(drift) + ". Run `checkpoint resume` and ask the user; "
                  "after a commit the user made or asked for, record it with `record --accept-head`.",
                  file=sys.stderr)
            return EXIT_REFUSED
    if args.action == "record":
        if args.accept_head:
            workflow.accept_head(project, cp)
        for section, values in (("Done", args.done), ("Tests run", args.test), ("Open issues", args.issue),
                                ("Approved scope", args.scope), ("Acceptance criteria", args.criterion)):
            if values and section in ("Approved scope", "Acceptance criteria") and \
                    cp["state"]["Phase"] not in ("intake", "research", "audit") and not args.user_approved_change:
                print(f"kit: refused: {section} changes after development started need "
                      "--user-approved-change", file=sys.stderr)
                return EXIT_REFUSED
            for value in values or []:
                cp[section].append(value.strip())
        if args.clear_issues:
            cp["Open issues"] = []
        if args.next:
            cp["Next step"] = args.next.strip()
        _save_cp(path, cp, project)
        print(f"recorded in {path.relative_to(project)}")
        return EXIT_OK
    if args.action == "event":
        verdict, findings, risk = args.verdict, list(args.finding or []), list(args.risk or [])
        if args.report:
            result = workflow.parse_result(read_text(args.report))
            expected = {"audit": "kit-auditor", "developed": "kit-developer", "review": "kit-reviewer"}
            if args.event in expected and result["role"] != expected[args.event]:
                print(f"kit: refused: a {result['role']} report cannot record '{args.event}'", file=sys.stderr)
                return EXIT_REFUSED
            verdict = verdict or result["verdict"]
            findings = findings or result["findings"]
            risk = risk or result["risk"]
        try:
            message = workflow.apply_event(project, config, cp, args.event, verdict, findings, risk,
                                           args.note or "")
        except KitError as exc:
            print(f"kit: refused: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        _save_cp(path, cp, project)
        print(f"{args.event}: {message}  status={cp['state']['Status']}  phase={cp['state']['Phase']}  "
              f"fix rounds={cp['state']['Fix rounds']}")
        print(f"Next step: {cp['Next step'] or '-'}")
        return EXIT_REFUSED if cp["state"]["Status"] in ("waiting_user", "stopped") else EXIT_OK
    raise KitError(f"unknown checkpoint action {args.action}")


def build_parser():
    parser = argparse.ArgumentParser(prog="kit.py", description="Developer Agent Kit")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="validate the kit source, or an installed project")
    p.add_argument("--project", help="project root to validate (default: the kit source, or this install)")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("check", help="validator plus the kit's tests (source tree)")
    p.add_argument("--project")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("install", help="install the kit into a project")
    p.add_argument("--target", required=True, help="project root (a git repository)")
    p.add_argument("--context", help="adapter PROJECT_CONTEXT.md to start from (default: the template)")
    p.add_argument("--force", action="store_true", help="replace kit files that differ")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("checkpoint", help="task checkpoints and orchestration gates")
    proj = argparse.ArgumentParser(add_help=False)
    proj.add_argument("--project", help="project root (default: this install)")
    csub = p.add_subparsers(dest="action", required=True)
    c = csub.add_parser("new", parents=[proj])
    c.add_argument("--task", required=True)
    c.add_argument("--goal", required=True)
    c.add_argument("--scope", action="append", help="approved file or glob (repeatable)")
    c.add_argument("--criterion", action="append", help="acceptance criterion (repeatable)")
    c.add_argument("--allow-dirty", action="store_true",
                   help="record existing uncommitted changes as a protected baseline")
    for name in ("verify", "resume", "show"):
        c = csub.add_parser(name, parents=[proj])
        c.add_argument("task", help="task slug or checkpoint path")
    c = csub.add_parser("record", parents=[proj])
    c.add_argument("task")
    for flag in ("--done", "--test", "--issue", "--scope", "--criterion"):
        c.add_argument(flag, action="append")
    c.add_argument("--next")
    c.add_argument("--clear-issues", action="store_true")
    c.add_argument("--accept-head", action="store_true",
                   help="record a commit the user made or asked for on the checkpoint's branch")
    c.add_argument("--user-approved-change", action="store_true",
                   help="the user explicitly approved changing scope or criteria mid-task")
    c = csub.add_parser("event", parents=[proj])
    c.add_argument("task")
    c.add_argument("event", choices=["research", "audit", "approve", "develop", "developed", "review", "stop"])
    c.add_argument("--verdict")
    c.add_argument("--report", help="agent report file; its kit-result block supplies verdict/findings/risk")
    c.add_argument("--finding", action="append")
    c.add_argument("--risk", action="append")
    c.add_argument("--note")
    p.set_defaults(func=cmd_checkpoint)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    layout = Layout()
    try:
        return args.func(args, layout)
    except KitRefused as exc:
        print(f"kit: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except KitError as exc:
        print(f"kit: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
