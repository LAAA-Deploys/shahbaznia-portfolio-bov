#!/usr/bin/env python3
"""Validate current spine content against its exact BOV approval record."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parent if (SCRIPT.parent / "reporting").is_dir() else SCRIPT.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reporting.bovsite.approval import (  # noqa: E402
    check_deal_record,
    check_portfolio_record,
    check_site_record,
)
from reporting.bovsite.contracts.workspace import DealWorkspace  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path, nargs="?")
    parser.add_argument("--site", type=Path, help="validate generated deal-repo artifacts")
    parser.add_argument("--record", type=Path, help="override the workspace approval record")
    parser.add_argument("--portfolio", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.site:
            if args.workspace or args.portfolio or args.record:
                raise ValueError("--site cannot be combined with workspace/--portfolio/--record")
            errors, warnings, _record = check_site_record(args.site.resolve())
        elif args.workspace is None:
            raise ValueError("workspace is required unless --site is used")
        else:
            workspace = DealWorkspace.from_path(args.workspace)
        if not args.site and args.portfolio:
            if args.record:
                raise ValueError("--record is not supported with --portfolio")
            errors, warnings, _record = check_portfolio_record(workspace)
        elif not args.site:
            errors, warnings, _record = check_deal_record(workspace, args.record)
    except Exception as exc:
        errors, warnings = [str(exc)], []

    print(f"BOV approval gate: {'PASS' if not errors else 'FAIL'}")
    for error in errors:
        print(f"  ERROR: {error}")
    for warning in warnings:
        print(f"  AUDIT WARNING: {warning}")
    if warnings and not errors:
        print("  Content approval remains valid; artifact hashes are audit-only.")
    # ADVISORY as of 2026-08-13 (June Mode): approval is the lead agent's
    # ordinary current-chat authorization, recorded as a non-gating audit
    # record. This reports what is missing and continues; it never blocks.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
