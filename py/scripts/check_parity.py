#!/usr/bin/env python3
"""Check parity between TypeScript and Python public APIs.

Usage:
    python scripts/check_parity.py [--ts-dir ../../src] [--py-package nanoclaw]

Compares exported symbols from TypeScript source files against Python module
__all__ / public names. Reports missing, extra, and name-mismatched items.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import re
import sys
from pathlib import Path


def extract_ts_exports(ts_dir: Path) -> dict[str, list[str]]:
    """Extract exported names from TypeScript files."""
    exports: dict[str, list[str]] = {}
    if not ts_dir.exists():
        print(f"WARNING: TypeScript directory not found: {ts_dir}")
        return exports

    for ts_file in sorted(ts_dir.rglob("*.ts")):
        if ts_file.name.endswith((".test.ts", ".d.ts")):
            continue
        names: list[str] = []
        content = ts_file.read_text(encoding="utf-8")
        # Match: export function/class/const/let/type/interface/enum NAME
        for m in re.finditer(
            r"export\s+(?:async\s+)?(?:function|class|const|let|var|type|interface|enum)\s+(\w+)",
            content,
        ):
            names.append(m.group(1))
        # Match: export { NAME, NAME2 }
        for m in re.finditer(r"export\s*\{([^}]+)\}", content):
            for name in m.group(1).split(","):
                name = name.strip().split(" as ")[-1].strip()
                if name:
                    names.append(name)
        if names:
            rel = ts_file.relative_to(ts_dir)
            exports[str(rel)] = sorted(set(names))
    return exports


def extract_py_public_api(package_name: str) -> dict[str, list[str]]:
    """Extract public API from Python package modules."""
    api: dict[str, list[str]] = {}
    try:
        pkg = importlib.import_module(package_name)
    except ImportError:
        print(f"WARNING: Cannot import {package_name}")
        return api

    pkg_path = getattr(pkg, "__path__", None)
    if pkg_path is None:
        return api

    for _importer, modname, _ispkg in pkgutil.walk_packages(pkg_path, prefix=f"{package_name}."):
        try:
            mod = importlib.import_module(modname)
        except Exception:  # noqa: BLE001
            continue
        public = getattr(mod, "__all__", None)
        if public is None:
            public = [n for n in dir(mod) if not n.startswith("_")]
        short_name = modname.removeprefix(f"{package_name}.")
        api[short_name] = sorted(public)
    return api


def to_snake_case(name: str) -> str:
    """Convert camelCase/PascalCase to snake_case."""
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def main() -> None:
    parser = argparse.ArgumentParser(description="Check TS/Python API parity")
    parser.add_argument("--ts-dir", type=Path, default=Path("../../src"), help="TypeScript src directory")
    parser.add_argument("--py-package", default="nanoclaw", help="Python package name")
    args = parser.parse_args()

    ts_exports = extract_ts_exports(args.ts_dir)
    py_api = extract_py_public_api(args.py_package)

    if not ts_exports:
        print("No TypeScript exports found. Check --ts-dir path.")
        sys.exit(0)

    all_ts_names: set[str] = set()
    for names in ts_exports.values():
        all_ts_names.update(names)

    all_py_names: set[str] = set()
    for names in py_api.values():
        all_py_names.update(names)

    # Convert TS names to snake_case for comparison
    ts_snake = {to_snake_case(n): n for n in all_ts_names}
    py_set = {n for n in all_py_names if not n.startswith("_")}

    missing = set(ts_snake.keys()) - py_set
    extra = py_set - set(ts_snake.keys())

    print(f"\n{'=' * 60}")
    print(f"TypeScript exports: {len(all_ts_names)}")
    print(f"Python public API:  {len(py_set)}")
    print(f"{'=' * 60}")

    if missing:
        print(f"\nMISSING in Python ({len(missing)}):")
        for name in sorted(missing):
            print(f"  - {name} (TS: {ts_snake[name]})")

    if extra:
        print(f"\nEXTRA in Python ({len(extra)}):")
        for name in sorted(extra):
            print(f"  + {name}")

    if not missing and not extra:
        print("\nParity check PASSED — all exports accounted for.")

    print()


if __name__ == "__main__":
    main()
