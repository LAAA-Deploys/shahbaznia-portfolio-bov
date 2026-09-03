"""Load and validate schema-versioned BOV spine documents."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


CONTRACT_DIR = Path(__file__).resolve().parent
DOCUMENT_FILES = {
    "deal": "deal.schema.json",
    "sources": "sources.schema.json",
    "financials": "financials.schema.json",
    "comps-sale": "comps-sale.schema.json",
    "comps-rent": "comps-rent.schema.json",
    "pricing": "pricing.schema.json",
    "copy": "copy.schema.json",
    "media-manifest": "media-manifest.schema.json",
    "exceptions": "exceptions.schema.json",
    "portfolio": "portfolio.schema.json",
    "approval-record": "approval-record.schema.json",
    # The public, hash-only binding a deal repository carries. Deliberately a
    # SEPARATE kind from "approval-record": the internal record is a workspace
    # document and a public deploy repository must never carry one.
    "site-approval": "site-approval.schema.json",
}


class ContractError(ValueError):
    """A spine file is absent, malformed, or fails its JSON Schema."""


def _schema(kind: str) -> dict[str, Any]:
    try:
        filename = DOCUMENT_FILES[kind]
    except KeyError as exc:
        raise ContractError(f"unknown BOV contract kind: {kind}") from exc
    return json.loads((CONTRACT_DIR / filename).read_text(encoding="utf-8"))


def validate_document(kind: str, document: Any) -> Any:
    validator = Draft202012Validator(_schema(kind), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(document), key=lambda error: list(error.absolute_path))
    if errors:
        rendered = []
        for error in errors:
            pointer = "/" + "/".join(str(part) for part in error.absolute_path)
            rendered.append(f"{pointer or '/'}: {error.message}")
        raise ContractError(f"{kind} contract failed:\n  " + "\n  ".join(rendered))
    return document


def load_document(path: str | Path, kind: str | None = None) -> Any:
    source = Path(path)
    if not source.is_file():
        raise ContractError(f"missing BOV contract: {source}")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid JSON in {source}: {exc}") from exc
    if kind:
        validate_document(kind, document)
    return document


def write_json_atomic(path: str | Path, document: Any) -> Path:
    """Write JSON through a same-directory temporary file and atomic replace."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination
