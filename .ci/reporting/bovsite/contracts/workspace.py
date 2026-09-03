"""Resolve a BOV workspace without hardcoded OneDrive or deal paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .loader import load_document


DATA_FILES = {
    "deal": "deal.json",
    "sources": "sources.json",
    "financials": "financials.json",
    "comps-sale": "comps-sale.json",
    "comps-rent": "comps-rent.json",
    "pricing": "pricing.json",
    "copy": "copy.json",
    "media-manifest": "media-manifest.json",
    "exceptions": "exceptions.json",
    "portfolio": "portfolio.json",
}


class WorkspaceError(ValueError):
    """The configured workspace escapes its declared local root."""


@dataclass(frozen=True)
class DealWorkspace:
    root: Path

    @classmethod
    def from_path(cls, path: str | Path) -> "DealWorkspace":
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise WorkspaceError(f"BOV workspace does not exist: {root}")
        return cls(root)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def review_root(self) -> Path:
        return self.root / "review-package"

    def path_for(self, kind: str) -> Path:
        try:
            return self.data_dir / DATA_FILES[kind]
        except KeyError as exc:
            raise WorkspaceError(f"unknown workspace document: {kind}") from exc

    def load(self, kind: str, *, required: bool = True) -> Any:
        path = self.path_for(kind)
        if not path.exists() and not required:
            return None
        return load_document(path, kind)

    def resolve_relative(self, raw: str, *, label: str = "path") -> Path:
        candidate = Path(raw)
        if candidate.is_absolute():
            raise WorkspaceError(f"{label} must be workspace-relative, not absolute: {raw}")
        resolved = (self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspaceError(f"{label} escapes the BOV workspace: {raw}") from exc
        return resolved

    def deal(self) -> dict[str, Any]:
        return self.load("deal")

    def configured_path(self, key: str) -> Path:
        deal = self.deal()
        layout = deal.get("workspace_layout") or {}
        if key not in layout:
            raise WorkspaceError(f"deal.json workspace_layout is missing {key!r}")
        return self.resolve_relative(layout[key], label=f"workspace_layout.{key}")

    @property
    def site_repo(self) -> Path:
        return self.configured_path("site_repo")

    @property
    def model_workbook(self) -> Path:
        return self.configured_path("model_workbook")

    @property
    def media_root(self) -> Path:
        return self.configured_path("media_root")
