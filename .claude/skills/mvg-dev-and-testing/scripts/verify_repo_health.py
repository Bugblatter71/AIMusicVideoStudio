#!/usr/bin/env python3
"""Verify MVG repository health: <850 line limits, py_compile, node -c JS syntax, and skills parity."""

from __future__ import annotations

import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
LINE_CEILING = 850


def main() -> int:
    work_dir = WORKSPACE_ROOT / "work"
    failures: list[str] = []

    # 1. Check <850 line limit across work/ (excluding Wan2GP)
    py_files: list[Path] = []
    js_files: list[Path] = []
    for p in sorted(work_dir.rglob("*")):
        if not p.is_file() or "Wan2GP" in p.parts or "__pycache__" in p.parts:
            continue
        sfx = p.suffix.lower()
        if sfx in (".py", ".js", ".html", ".css"):
            n_lines = len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
            if n_lines >= LINE_CEILING:
                failures.append(f"LINE_LIMIT: {p.relative_to(WORKSPACE_ROOT).as_posix()} has {n_lines} lines (must be < {LINE_CEILING})")
        if sfx == ".py":
            py_files.append(p)
        elif sfx == ".js" and p.parent.name == "js":
            js_files.append(p)

    # Also compile skill scripts
    for sp in sorted((WORKSPACE_ROOT / ".agents" / "skills").rglob("*.py")):
        if "__pycache__" not in sp.parts:
            py_files.append(sp)

    # 2. py_compile
    for pf in py_files:
        try:
            py_compile.compile(str(pf), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append(f"PY_COMPILE: {pf.relative_to(WORKSPACE_ROOT).as_posix()} -> {exc}")

    # 3. node -c on frontend JS
    node_bin = shutil.which("node")
    if node_bin:
        for jf in js_files:
            res = subprocess.run([node_bin, "-c", str(jf)], capture_output=True, text=True)
            if res.returncode != 0:
                failures.append(f"NODE_SYNTAX: {jf.relative_to(WORKSPACE_ROOT).as_posix()} -> {(res.stderr or res.stdout).strip()}")
    else:
        print("[WARN] node binary not found; skipped JS syntax checks.")

    # 4. Skills parity (.agents/skills/<name>/SKILL.md <-> .claude/skills/<name>/SKILL.md)
    agents_skills = WORKSPACE_ROOT / ".agents" / "skills"
    claude_skills = WORKSPACE_ROOT / ".claude" / "skills"
    if agents_skills.is_dir():
        for sd in sorted(agents_skills.iterdir()):
            if not sd.is_dir():
                continue
            src_md = sd / "SKILL.md"
            dst_md = claude_skills / sd.name / "SKILL.md"
            if not src_md.is_file():
                failures.append(f"SKILL_MISSING: {src_md.relative_to(WORKSPACE_ROOT).as_posix()}")
            elif not dst_md.is_file():
                failures.append(f"CLAUDE_SKILL_MISSING: {dst_md.relative_to(WORKSPACE_ROOT).as_posix()}")
            elif src_md.read_text(encoding="utf-8") != dst_md.read_text(encoding="utf-8"):
                failures.append(f"SKILL_DRIFT: {sd.name}/SKILL.md differs between .agents/skills and .claude/skills")

    print("=" * 80)
    print("  MVG Repo Health Verification")
    print("=" * 80)
    print(f"  Python files compiled : {len(py_files)}")
    print(f"  JS modules checked    : {len(js_files)}")
    if failures:
        print(f"  Status                : FAILED ({len(failures)} issues)")
        for f in failures:
            print(f"   - {f}")
        print("=" * 80)
        return 1
    print("  Status                : PASS (all limits, syntax, and skill sync checks OK)")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    sys.exit(main())
