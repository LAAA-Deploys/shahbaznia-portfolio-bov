"""Compile annotated BOV copy Markdown into the canonical ``copy.json``.

Authoring format::

    ## investment.overview
    <!-- claim id=overview-1 type=verified_fact support=sources:assessor-1 -->
    The property contains ten apartment units.

    <!-- claim id=overview-2 type=broker_opinion support=comps-sale:conclusions
         framed_as="broker opinion" -->
    The asset is positioned for private multifamily investors.

Each client-facing paragraph has exactly one annotation.  This makes omission
fail closed without asking an NLP model to guess whether prose is factual.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import re
import shlex

from reporting.bovsite.contracts.loader import validate_document, write_json_atomic


CLAIM_TYPES = {
    "verified_fact",
    "sourced_market_fact",
    "derived_calculation",
    "broker_opinion",
    "projection_scenario",
    "unverified",
}
ANNOTATION = re.compile(r"^<!--\s*claim\s+(.+?)\s*-->$")
BLOCK = re.compile(r"^##\s+([a-z0-9_-]+)\.([a-z0-9_-]+)\s*$")


class CopyCompileError(ValueError):
    pass


def _annotation(text: str, line_number: int) -> dict:
    match = ANNOTATION.match(text.strip())
    if not match:
        raise CopyCompileError(f"line {line_number}: malformed claim annotation")
    try:
        tokens = shlex.split(match.group(1))
    except ValueError as exc:
        raise CopyCompileError(f"line {line_number}: {exc}") from exc
    values: dict[str, str] = {}
    for token in tokens:
        if "=" not in token:
            raise CopyCompileError(f"line {line_number}: annotation token lacks '=': {token}")
        key, value = token.split("=", 1)
        if key in values:
            raise CopyCompileError(f"line {line_number}: duplicate annotation field {key}")
        values[key] = value
    required = {"id", "type", "support"}
    missing = required - values.keys()
    if missing:
        raise CopyCompileError(
            f"line {line_number}: annotation missing {', '.join(sorted(missing))}"
        )
    unknown = values.keys() - {"id", "type", "support", "framed_as"}
    if unknown:
        raise CopyCompileError(
            f"line {line_number}: annotation has unknown fields {sorted(unknown)}"
        )
    if values["type"] not in CLAIM_TYPES:
        raise CopyCompileError(f"line {line_number}: unsupported claim type {values['type']!r}")
    support_refs = [] if values["support"] == "-" else values["support"].split(",")
    return {
        "id": values["id"],
        "claim_type": values["type"],
        "support_refs": support_refs,
        "framed_as": values.get("framed_as") or None,
    }


def compile_text(text: str, deal_id: str) -> dict:
    blocks: dict[str, dict[str, list[str]]] = {}
    claims: list[dict] = []
    seen_claims: set[str] = set()
    current: tuple[str, str] | None = None
    pending: tuple[dict, int] | None = None
    paragraph: list[str] = []
    paragraph_line = 0

    def flush() -> None:
        nonlocal pending, paragraph, paragraph_line
        if not paragraph:
            return
        if current is None:
            raise CopyCompileError(
                f"line {paragraph_line}: client-facing copy appears before a ## section.block"
            )
        if pending is None:
            raise CopyCompileError(
                f"line {paragraph_line}: unannotated client-facing paragraph"
            )
        claim, _annotation_line = pending
        rendered = " ".join(part.strip() for part in paragraph).strip()
        claim["text"] = rendered
        if claim["id"] in seen_claims:
            raise CopyCompileError(f"duplicate claim id {claim['id']!r}")
        seen_claims.add(claim["id"])
        claims.append(claim)
        section, key = current
        blocks.setdefault(section, {}).setdefault(key, []).append(rendered)
        pending = None
        paragraph = []
        paragraph_line = 0

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        line_number = index + 1
        block_match = BLOCK.match(stripped)
        if block_match:
            flush()
            if pending is not None:
                raise CopyCompileError(
                    f"line {pending[1]}: claim annotation has no following paragraph"
                )
            current = (block_match.group(1), block_match.group(2))
        elif stripped.startswith("# ") or not stripped:
            flush()
        elif stripped.startswith("<!--"):
            flush()
            if pending is not None:
                raise CopyCompileError(
                    f"line {pending[1]}: claim annotation has no following paragraph"
                )
            annotation_lines = [stripped]
            while "-->" not in annotation_lines[-1]:
                index += 1
                if index >= len(lines):
                    raise CopyCompileError(f"line {line_number}: unterminated annotation")
                annotation_lines.append(lines[index].strip())
            pending = (_annotation(" ".join(annotation_lines), line_number), line_number)
        else:
            if not paragraph:
                paragraph_line = line_number
            paragraph.append(stripped)
        index += 1
    flush()
    if pending is not None:
        raise CopyCompileError(f"line {pending[1]}: claim annotation has no following paragraph")
    document = {
        "schema_version": "1.0.0",
        "deal_id": deal_id,
        "blocks": blocks,
        "claims": claims,
    }
    validate_document("copy", document)
    return document


def publication_errors(document: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["copy contract is not an object"]
    claims = document.get("claims")
    blocks = document.get("blocks")
    if not isinstance(claims, list) or not isinstance(blocks, dict):
        return ["copy contract is missing blocks or claims"]
    claim_ids = [claim["id"] for claim in claims]
    if len(claim_ids) != len(set(claim_ids)):
        errors.append("copy.json contains duplicate claim ids")
    block_texts = [
        paragraph
        for fields in blocks.values()
        for paragraphs in fields.values()
        for paragraph in paragraphs
    ]
    claim_texts = [claim["text"] for claim in claims]
    if Counter(block_texts) != Counter(claim_texts):
        errors.append(
            "copy blocks and claims must contain the same client-facing paragraphs exactly once"
        )
    for claim in claims:
        claim_id = claim["id"]
        kind = claim["claim_type"]
        if kind == "unverified":
            errors.append(f"{claim_id}: unverified claims cannot publish client-facing")
            continue
        if not claim["support_refs"]:
            errors.append(f"{claim_id}: {kind} claim needs at least one support ref")
        if kind in {"broker_opinion", "projection_scenario"} and not claim["framed_as"]:
            errors.append(f"{claim_id}: {kind} must record how it is framed")
    return errors


def _path_exists(document, target: str) -> bool:
    if not target:
        return False
    if target.startswith("#/"):
        parts = [part.replace("~1", "/").replace("~0", "~") for part in target[2:].split("/")]
    else:
        parts = target.split(".")
    current = document
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list):
            if part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                match = next(
                    (item for item in current if isinstance(item, dict) and item.get("id") == part),
                    None,
                )
                if match is None:
                    return False
                current = match
        else:
            return False
    return True


def support_reference_exists(reference: str, workspace) -> bool:
    """Return whether one structured reference resolves in the current spine."""
    if ":" not in reference:
        return False
    kind, target = reference.split(":", 1)
    sources_document = workspace.load("sources")
    sources = {record["id"] for record in sources_document["records"]}
    deal = workspace.load("deal")
    financials = workspace.load("financials")
    pricing = workspace.load("pricing")
    sale = workspace.load("comps-sale")
    rent = workspace.load("comps-rent")
    sale_refs = {row["id"] for row in sale["rows"]} | {"conclusions"}
    rent_refs = {row["id"] for row in rent["rows"]} | {"conclusions"}
    if kind == "deal":
        return target == "identity" or _path_exists(deal, target)
    if kind == "financials":
        return _path_exists(financials, target)
    if kind == "pricing":
        return _path_exists(pricing, target)
    if kind == "sources":
        return target in sources
    if kind == "comps-sale":
        return target in sale_refs
    if kind == "comps-rent":
        return target in rent_refs
    return False


def support_reference_errors(document: dict, workspace) -> list[str]:
    """Prove claim references resolve into the current structured spine."""
    errors = []
    if document.get("deal_id") != workspace.load("deal")["deal_id"]:
        errors.append("copy.json deal_id does not match deal.json")
    for claim in document["claims"]:
        for reference in claim["support_refs"]:
            if not support_reference_exists(reference, workspace):
                errors.append(f"{claim['id']}: unresolved support ref {reference!r}")
    return errors


def compile_file(source: Path, destination: Path, deal_id: str) -> dict:
    document = compile_text(source.read_text(encoding="utf-8"), deal_id)
    write_json_atomic(destination, document)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--deal-id", required=True)
    args = parser.parse_args(argv)
    try:
        document = compile_file(args.source, args.destination, args.deal_id)
    except (OSError, CopyCompileError, ValueError) as exc:
        print(f"BOV copy compile FAIL: {exc}")
        return 1
    errors = publication_errors(document)
    if errors:
        print("BOV copy compile completed; publication is blocked:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"BOV copy compile PASS: {len(document['claims'])} annotated claims")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
