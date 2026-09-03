"""Deterministic media rights, identity, parity, and image-quality gate.

Resolution rules are based on rendered size rather than one arbitrary pixel
floor.  A hero may not be upscaled (1.00x maximum).  Other published images may
be upscaled by at most 1.25x in either dimension.  Perceptual-hash distance 0 is
an exact visual duplicate and fails; distances 1-4 are near-duplicate warnings
for human curation.  No recognition model is used.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from PIL import Image

from reporting.bovsite.contracts.canonical import file_sha256
from reporting.bovsite.contracts.loader import load_document
from reporting.bovsite.contracts.workspace import DealWorkspace, WorkspaceError
from reporting.bovsite.site_artifacts import SITE_APPROVAL_FILENAME

from .common import CheckReport, run_cli


HERO_MAX_UPSCALE = 1.0
OTHER_MAX_UPSCALE = 1.25
NEAR_DUPLICATE_DISTANCE = 4


def _imagehash_module():
    try:
        import imagehash
    except ImportError as exc:  # pragma: no cover - dependency failure is explicit
        raise RuntimeError(
            "ImageHash is required by the BOV media gate; install the pinned BOV requirements"
        ) from exc
    return imagehash


def _location(media_root: Path, item: dict) -> Path:
    folder = {
        "published": "published",
        "rejected": "rejected",
        "research_only": "archive",
        "publication_candidate": "archive",
    }[item["status"]]
    return media_root / folder / item["filename"]


def check(workspace: DealWorkspace) -> CheckReport:
    report = CheckReport("media")
    deal = workspace.load("deal")
    manifest = workspace.load("media-manifest")
    exceptions = workspace.load("exceptions")
    if manifest["deal_id"] != deal["deal_id"]:
        report.error("deal-id", "media-manifest.json deal_id does not match deal.json")
    approved_exceptions = {
        item["id"]: item
        for item in exceptions["items"]
        if item["approved"] and item["status"] == "resolved"
    }
    media_root = workspace.media_root
    if not media_root.is_dir():
        report.error("media-root", f"media root does not exist: {media_root}")
        return report

    imagehash = _imagehash_module()
    items = manifest["archive"]
    ids: set[str] = set()
    filenames: set[str] = set()
    declared_paths: set[Path] = set()
    hashes: dict[str, list[str]] = defaultdict(list)
    phashes: list[tuple[str, object]] = []
    by_id: dict[str, dict] = {}

    for item in items:
        item_id = item["id"]
        by_id[item_id] = item
        if item_id in ids:
            report.error("duplicate-id", f"duplicate media id {item_id!r}")
        ids.add(item_id)
        if item["filename"] in filenames:
            report.error("duplicate-filename", f"duplicate filename {item['filename']!r}")
        filenames.add(item["filename"])
        if Path(item["filename"]).name != item["filename"]:
            report.error("filename-path", f"{item_id}: filename must be a basename")
            continue

        path = _location(media_root, item).resolve()
        try:
            path.relative_to(media_root.resolve())
        except ValueError:
            report.error("media-path", f"{item_id}: resolved path escapes media root")
            continue
        declared_paths.add(path)
        if not path.is_file():
            report.error("missing-file", f"{item_id}: missing {path}")
            continue
        actual_sha = file_sha256(path)
        if actual_sha != item["sha256"]:
            report.error("sha256", f"{item_id}: file SHA-256 does not match manifest")
        hashes[actual_sha].append(item_id)
        try:
            with Image.open(path) as image:
                if (image.width, image.height) != (item["width"], item["height"]):
                    report.error("dimensions", f"{item_id}: dimensions do not match manifest")
                actual_phash = imagehash.phash(image)
        except (OSError, ValueError) as exc:
            report.error("invalid-image", f"{item_id}: cannot decode image: {exc}")
            continue
        if str(actual_phash) != item["phash"]:
            report.error("phash", f"{item_id}: perceptual hash does not match manifest")
        phashes.append((item_id, actual_phash))

        if item["property_match"] == "confirmed_human" and not (
            item["match_basis"] and item["reviewed_by"] and item["reviewed_at"]
        ):
            report.error(
                "match-evidence", f"{item_id}: human confirmation lacks basis/reviewer/time"
            )
        if item["stock_or_illustrative"] and not item["stock_basis"]:
            report.error(
                "stock-basis", f"{item_id}: stock/illustrative classification needs a basis"
            )
        if item["status"] == "rejected" and not item["rejected_reason"]:
            report.error("rejected-reason", f"{item_id}: rejected media needs a reason")

    for digest, duplicate_ids in hashes.items():
        if len(duplicate_ids) > 1:
            report.error(
                "exact-duplicate",
                f"identical file content appears as {', '.join(sorted(duplicate_ids))}",
            )
    for index, (left_id, left_hash) in enumerate(phashes):
        for right_id, right_hash in phashes[index + 1 :]:
            distance = left_hash - right_hash
            if distance == 0:
                report.error(
                    "visual-duplicate", f"{left_id} and {right_id} have phash distance 0"
                )
            elif distance <= NEAR_DUPLICATE_DISTANCE:
                report.warn(
                    "near-duplicate",
                    f"{left_id} and {right_id} have phash distance {distance}; curate once",
                )

    actual_paths = {
        path.resolve()
        for folder in ("archive", "published", "rejected")
        for path in (media_root / folder).glob("**/*")
        if path.is_file()
    }
    for undeclared in sorted(actual_paths - declared_paths):
        report.error(
            "disk-parity", f"file is on disk but absent from manifest: {undeclared}"
        )
    for missing in sorted(declared_paths - actual_paths):
        report.error("disk-parity", f"manifest file is absent from disk: {missing}")

    published = manifest["published"]
    order = published["order"]
    published_ids = {item["id"] for item in items if item["status"] == "published"}
    if len(order) != len(set(order)):
        report.error("published-order", "published order contains duplicate ids")
    if set(order) != published_ids:
        report.error(
            "published-parity", "published order must contain every and only published item"
        )
    hero = published["hero"]
    no_hero = any(
        item["materiality"] == "no_safe_hero"
        for item in approved_exceptions.values()
    )
    if hero is None and not no_hero:
        report.error("hero", "published media needs a hero or approved no-safe-hero exception")
    if hero is not None and hero not in order:
        report.error("hero-order", "hero must appear in published order")

    for item_id in order:
        item = by_id.get(item_id)
        if item is None:
            report.error("published-ref", f"unknown published media id {item_id!r}")
            continue
        if item["property_match"] != "confirmed_human":
            report.error(
                "property-match", f"{item_id}: published image is not human-confirmed"
            )
        if item["stock_or_illustrative"] and (
            item.get("exception_id") not in approved_exceptions
        ):
            report.error(
                "stock-publication",
                f"{item_id}: a stock or illustrative image is not this property; "
                f"publishing one needs a recorded approved exception",
            )
        # publication_basis is recorded for provenance but never gates a build.
        # A BOV publishes photographs of the client's own building into a
        # document prepared for that client, so there is no permission to seek
        # (Glen, 2026-08-18). property_match above still gates: publishing
        # ANOTHER building's photo is a wrong-fact defect, not a rights one.
        rendered_width = item.get("rendered_width")
        rendered_height = item.get("rendered_height")
        if rendered_width is None or rendered_height is None:
            report.error(
                "rendered-size", f"{item_id}: published media needs rendered dimensions"
            )
            continue
        limit = HERO_MAX_UPSCALE if item_id == hero else OTHER_MAX_UPSCALE
        if rendered_width / item["width"] > limit or rendered_height / item["height"] > limit:
            report.error(
                "upscale",
                f"{item_id}: rendered size exceeds {limit:.2f}x upscale limit"
                + (" for hero" if item_id == hero else ""),
            )
    return report


def check_site(site: Path) -> CheckReport:
    """Validate only the published media copied into a deal repository."""
    report = CheckReport("media-site")
    manifest = load_document(site / "media-manifest.json", "media-manifest")
    # The PUBLIC binding, never the internal record. This gate needs one fact
    # per exception, whether it was approved, and an id carries that. The
    # description that explains the ruling is deliberation and stays in the
    # workspace.
    approval = load_document(site / SITE_APPROVAL_FILENAME, "site-approval")
    approved_exception_ids = set(approval["approved_exception_ids"])
    by_id = {item["id"]: item for item in manifest["archive"]}
    imagehash = _imagehash_module()
    observed: list[tuple[str, object, str]] = []
    order = manifest["published"]["order"]
    ids = [item["id"] for item in manifest["archive"]]
    filenames = [item["filename"] for item in manifest["archive"]]
    if len(ids) != len(set(ids)):
        report.error("duplicate-id", "published media manifest contains duplicate ids")
    if len(filenames) != len(set(filenames)):
        report.error("duplicate-filename", "published media manifest contains duplicate filenames")
    if approval["deal_id"] != manifest["deal_id"]:
        report.error("deal-id", "media manifest and approval record deal_id differ")
    if set(order) != {item["id"] for item in manifest["archive"] if item["status"] == "published"}:
        report.error("published-parity", "published order and manifest status disagree")
    for item_id in order:
        item = by_id.get(item_id)
        if item is None:
            report.error("published-ref", f"unknown published id {item_id!r}")
            continue
        path = site / "images" / item["filename"]
        if not path.is_file():
            report.error("missing-file", f"published image is missing: {path}")
            continue
        if file_sha256(path) != item["sha256"]:
            report.error("sha256", f"{item_id}: published bytes differ from manifest")
        try:
            with Image.open(path) as image:
                if (image.width, image.height) != (item["width"], item["height"]):
                    report.error("dimensions", f"{item_id}: dimensions differ from manifest")
                phash = imagehash.phash(image)
        except OSError as exc:
            report.error("invalid-image", f"{item_id}: {exc}")
            continue
        if str(phash) != item["phash"]:
            report.error("phash", f"{item_id}: perceptual hash differs from manifest")
        observed.append((item_id, phash, item["sha256"]))
        if item["property_match"] != "confirmed_human":
            report.error(
                "publication-contract",
                f"{item_id}: publication needs a human-confirmed property match",
            )
        limit = HERO_MAX_UPSCALE if item_id == manifest["published"]["hero"] else OTHER_MAX_UPSCALE
        if item.get("rendered_width") is None or item.get("rendered_height") is None:
            report.error("rendered-size", f"{item_id}: rendered dimensions are missing")
        elif item["rendered_width"] / item["width"] > limit or item["rendered_height"] / item["height"] > limit:
            report.error("upscale", f"{item_id}: exceeds {limit:.2f}x upscale limit")
    for index, (left_id, left_hash, left_sha) in enumerate(observed):
        for right_id, right_hash, right_sha in observed[index + 1 :]:
            if left_sha == right_sha or left_hash - right_hash == 0:
                report.error("exact-duplicate", f"{left_id} and {right_id} are duplicates")
            elif left_hash - right_hash <= NEAR_DUPLICATE_DISTANCE:
                report.warn("near-duplicate", f"{left_id} and {right_id} need curation review")
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path, nargs="?")
    parser.add_argument("--site", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        if args.site:
            if args.workspace:
                raise ValueError("--site cannot be combined with workspace")
            report = check_site(args.site.resolve())
        elif args.workspace:
            report = check(DealWorkspace.from_path(args.workspace))
        else:
            raise ValueError("workspace is required unless --site is used")
    except Exception as exc:
        report = CheckReport("media")
        report.error("contract", str(exc))
    print(json.dumps(report.as_dict(), indent=2) if args.as_json else report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
