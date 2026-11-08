#!/usr/bin/env python3
"""MVG Symbol Indexer & Code Navigator.

Extracts functions, classes, and methods with precise line ranges across:
- work/pipeline/*.py
- work/ui/*.py
- work/ui/static/js/*.js

Enables subagents and models to locate code symbols without reading entire multi-thousand-line files.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent


def scan_python_file(path: Path) -> list[dict]:
    symbols = []
    try:
        content = path.read_text(encoding="utf-8")
        tree = ast.parse(content, filename=str(path))
    except Exception as e:
        return [{"name": f"<parse-error: {e}>", "type": "error", "line_start": 0, "line_end": 0, "doc": ""}]

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node) or ""
            doc_line = doc.splitlines()[0].strip() if doc else ""
            symbols.append({
                "name": node.name,
                "type": "function",
                "line_start": node.lineno,
                "line_end": getattr(node, "end_lineno", node.lineno),
                "doc": doc_line,
            })
        elif isinstance(node, ast.ClassDef):
            doc = ast.get_docstring(node) or ""
            doc_line = doc.splitlines()[0].strip() if doc else ""
            symbols.append({
                "name": node.name,
                "type": "class",
                "line_start": node.lineno,
                "line_end": getattr(node, "end_lineno", node.lineno),
                "doc": doc_line,
            })
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_doc = ast.get_docstring(item) or ""
                    m_doc_line = m_doc.splitlines()[0].strip() if m_doc else ""
                    symbols.append({
                        "name": f"{node.name}.{item.name}",
                        "type": "method",
                        "line_start": item.lineno,
                        "line_end": getattr(item, "end_lineno", item.lineno),
                        "doc": m_doc_line,
                    })
    return symbols


def scan_javascript_file(path: Path) -> list[dict]:
    symbols = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return [{"name": f"<read-error: {e}>", "type": "error", "line_start": 0, "line_end": 0, "doc": ""}]

    fn_regex = re.compile(r"^(?:async\s+)?function\s+([a-zA-Z0-9_$]+)\s*\(")
    const_fn_regex = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([a-zA-Z0-9_$]+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[a-zA-Z0-9_$]+)\s*=>")
    class_regex = re.compile(r"^(?:export\s+)?class\s+([a-zA-Z0-9_$]+)")

    for i, line in enumerate(lines, 1):
        sline = line.strip()
        m = fn_regex.match(sline) or const_fn_regex.match(sline)
        if m:
            doc = ""
            if i > 1 and lines[i - 2].strip().startswith("//"):
                doc = lines[i - 2].strip().lstrip("/ *").strip()
            symbols.append({
                "name": m.group(1),
                "type": "function",
                "line_start": i,
                "line_end": i,  # Approximate
                "doc": doc,
            })
            continue

        mc = class_regex.match(sline)
        if mc:
            symbols.append({
                "name": mc.group(1),
                "type": "class",
                "line_start": i,
                "line_end": i,
                "doc": "",
            })
    return symbols


def build_full_index(root: Path) -> dict[str, dict]:
    index = {}
    target_dirs = [
        root / "work" / "pipeline",
        root / "work" / "ui",
        root / "work" / "ui" / "static" / "js",
    ]

    for tdir in target_dirs:
        if not tdir.is_dir():
            continue
        for p in sorted(tdir.iterdir()):
            if p.is_file():
                if p.suffix == ".py":
                    rel = p.relative_to(root).as_posix()
                    index[rel] = {
                        "path": rel,
                        "lines": len(p.read_text(encoding="utf-8").splitlines()),
                        "symbols": scan_python_file(p),
                    }
                elif p.suffix == ".js":
                    rel = p.relative_to(root).as_posix()
                    index[rel] = {
                        "path": rel,
                        "lines": len(p.read_text(encoding="utf-8").splitlines()),
                        "symbols": scan_javascript_file(p),
                    }
    return index


def main():
    parser = argparse.ArgumentParser(description="MVG Symbol Indexer & Code Navigator")
    parser.add_argument("--module", "-m", help="Filter by module / filename substring")
    parser.add_argument("--find", "-f", help="Find a specific symbol across all modules")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    index = build_full_index(WORKSPACE_ROOT)

    if args.find:
        query = args.find.lower()
        matches = []
        for file_path, file_info in index.items():
            for sym in file_info["symbols"]:
                if query in sym["name"].lower():
                    matches.append({
                        "symbol": sym["name"],
                        "type": sym["type"],
                        "file": file_path,
                        "lines": f"L{sym['line_start']}-L{sym['line_end']}",
                        "doc": sym["doc"],
                    })

        if args.json:
            print(json.dumps(matches, indent=2))
        else:
            print(f"Found {len(matches)} matches for '{args.find}':")
            print("-" * 80)
            for m in matches:
                doc_str = f" - {m['doc']}" if m["doc"] else ""
                print(f"[{m['type']:8}] {m['symbol']:32} -> {m['file']} ({m['lines']}){doc_str}")
        return

    if args.module:
        mod_q = args.module.lower()
        filtered = {k: v for k, v in index.items() if mod_q in k.lower()}
        if args.json:
            print(json.dumps(filtered, indent=2))
        else:
            for file_path, file_info in filtered.items():
                print(f"\n📄 {file_path} ({file_info['lines']} lines)")
                print("-" * 80)
                for sym in file_info["symbols"]:
                    doc_str = f" : {sym['doc']}" if sym["doc"] else ""
                    print(f"  L{sym['line_start']:<4} [{sym['type'][:4]}] {sym['name']}{doc_str}")
        return

    if args.json:
        print(json.dumps(index, indent=2))
        return

    print("=" * 80)
    print("  MVG Codebase Architecture & Symbol Index")
    print("=" * 80)
    total_symbols = 0
    total_lines = 0
    for file_path, file_info in index.items():
        sym_count = len(file_info["symbols"])
        total_symbols += sym_count
        total_lines += file_info["lines"]
        print(f"📄 {file_path:<40} {file_info['lines']:>5} lines | {sym_count:>3} symbols")

    print("-" * 80)
    print(f"Total: {len(index)} files, {total_lines} lines, {total_symbols} symbols indexed.")
    print("Use --find <symbol> or --module <name> to inspect individual components.\n")


if __name__ == "__main__":
    main()
