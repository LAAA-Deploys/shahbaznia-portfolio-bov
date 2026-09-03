"""Canonical serialization used for BOV approval validity.

Approval hashes describe reviewed meaning, not ZIP metadata, file timestamps, or
formatting noise.  Strings are Unicode-normalized and whitespace-normalized;
finite numeric values are reduced so ``1``, ``1.0`` and ``1.000`` serialize the
same way.  Dict keys are sorted and JSON is emitted without insignificant
spacing.  Raw artifact hashes remain available separately for audit only.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import unicodedata
from typing import Any


_WHITESPACE = re.compile(r"\s+")


def _normalize_number(value: int | float) -> int | float:
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON cannot contain NaN or infinity")
    try:
        number = Decimal(str(value)).normalize()
    except InvalidOperation as exc:
        raise ValueError(f"invalid canonical number: {value!r}") from exc
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def normalize(value: Any) -> Any:
    """Return a recursively normalized, JSON-serializable value."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return _normalize_number(value)
    if isinstance(value, str):
        text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
        return _WHITESPACE.sub(" ", text).strip()
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize(value[key]) for key in sorted(value, key=lambda item: str(item))}
    raise TypeError(f"unsupported canonical value type: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
