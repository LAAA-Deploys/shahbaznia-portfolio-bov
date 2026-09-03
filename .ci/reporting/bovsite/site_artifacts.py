"""What a PUBLIC deal repository is allowed to contain, and what it never is.

The rule, stated once: **an internal deliberation artifact never ships to a
public deploy target.** Not because of which strings it happens to contain, but
because of what kind of file it is. Approver utterances, pricing confidence
levels, media rejection notes and internal support references are the record of
how a decision was reached. The deploy repository exists to serve a website.

Every `{slug}-bov` deploy repository is public. `raw.githubusercontent.com`
serves every tracked file in it, unauthenticated, whether or not GitHub Pages
publishes that file. The deploy workflow already stages an allowlist into
`_site`, so the published SITE was never the defect. The REPOSITORY was:
measured live on 2026-08-07, the internal `approval-record.json` was readable at
`raw.githubusercontent.com/LAAA-Deploys/11747-moorpark-bov/master/approval-record.json`.
Meanwhile the same build's site gate fails a PAGE that prints a confidence
level (`check_bov_site.py`, check 11b) while the build shipped that confidence
level to a public repository in JSON.

This is NOT a content scrubber, and must never become one. Property addresses
and escrow references are legitimate BOV substance: Glen's ruling, 2026-08-08,
is that a comparable deal in escrow is valuable pricing evidence and evidence of
an active market presence, and naming one is fine. Nothing here inspects a
string for sensitivity, and nothing here redacts an address.

Two controls live in this module, and they are deliberately independent:

1. **Class.** `public_approval_record` projects the internal approval record
   down to the binding evidence a server-side gate reads back: content hashes,
   the approved price value, the approver, and the scope. Deliberation fields
   stay in the workspace. The public file has its own name and its own schema,
   so an internal record dropped into a deploy tree fails on BOTH counts.

2. **Scope.** `unexpected_files` is an ALLOWLIST, not a blocklist. A file nobody
   thought about is refused by default rather than published by default. That is
   the whole difference: a blocklist would have had to predict
   `approval-record.json` in advance, and nobody did.

This module is stdlib-only on purpose. `scripts/check_bov_site.py` loads it by
file path from three layouts (repo, the `.ci/` bundle inside a deal repo, and
the laaa-core plugin), and that gate must not acquire a package dependency.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable


#: The public, hash-only approval binding that ships in the deploy repo.
SITE_APPROVAL_FILENAME = "approval-binding.json"

#: The INTERNAL approval record. Workspace only. Named here so the scope gate
#: can say exactly what it found rather than "unexpected file".
INTERNAL_APPROVAL_FILENAME = "approval-record.json"

#: Exact filenames a deploy repository may carry at its root.
SITE_ROOT_FILES = frozenset(
    {
        "index.html",
        "CNAME",
        ".nojekyll",
        ".gitignore",
        "og.jpg",
        "robots.txt",
        "bov-site.json",
        "media-manifest.json",
        "map-manifest.json",
        SITE_APPROVAL_FILENAME,
    }
)

#: The build's own record of every scaffold file it wrote, written by
#: `build_bov_site.scaffold`. The gate allows exactly those paths.
#:
#: An earlier draft allowed the `.ci/` and `.github/` trees WHOLESALE, by prefix.
#: Both reviewers caught it on 2026-08-08 and they were right: `scaffold()`
#: replaces named files, so `.ci/internal-notes.json` survived every rebuild and
#: rode the prefix straight into the public repository. An allowlist with two
#: unbounded trees inside it is not an allowlist. The manifest keeps the rule in
#: one place and cannot drift from what the build actually generates.
GENERATED_MANIFEST = ".ci/generated-manifest.json"

#: The one workflow file the design lock requires, named exactly. `.github/` is
#: NOT a permitted tree: another workflow there is a deliberate decision, not a
#: generated artifact.
DEPLOY_WORKFLOW = ".github/workflows/deploy-pages.yml"

#: The CLOSED grammar of scaffold paths, mirroring what `build_bov_site.scaffold`
#: writes. Keys are directories; values are exact filenames, or suffixes when
#: they begin with a dot.
#:
#: The manifest alone was not enough. A manifest read from the tree it audits is
#: SELF-AUTHORIZING: add `internal-notes.json` and add that same string to
#: `.ci/generated-manifest.json`, and the gate allowed it, because the deploy
#: workflow runs `python .ci/check_bov_site.py .` and never rebuilds the
#: manifest (Codex P1, PR #109 round 2, reproduced). A path must now satisfy
#: BOTH this grammar and the manifest, so the manifest can only ever NARROW the
#: allowlist and never widen it. `test_the_scaffold_grammar_accepts_every_generated_path`
#: fails the moment scaffold() writes something this does not describe.
SCAFFOLD_GRAMMAR: dict[str, frozenset[str]] = {
    ".ci": frozenset({"check_bov_site.py", "check_bov_approval.py", "requirements.txt", "README.md"}),
    ".ci/audits": frozenset({".mjs"}),
    ".ci/reporting/bovsite": frozenset({".py"}),
    ".ci/reporting/bovsite/contracts": frozenset({".py", ".schema.json"}),
    ".ci/reporting/bovsite/checks": frozenset({".py"}),
    ".github/workflows": frozenset({"deploy-pages.yml"}),
}


def is_scaffold_path(path: str) -> bool:
    """Whether a path is shaped like something the build's scaffold writes."""
    parent, _, name = path.rpartition("/")
    allowed = SCAFFOLD_GRAMMAR.get(parent)
    if allowed is None or not name:
        return False
    return name in allowed or any(
        entry.startswith(".") and name.endswith(entry) for entry in allowed
    )

#: Only real image bytes belong under `images/`. A JSON or PDF parked there
#: would otherwise ride the tree allowance straight into the public repo.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".svg", ".ico"})

#: Local build scratch, never committed (the generated `.gitignore` covers
#: them). Only the filesystem fallback consults this; on a real repository git
#: answers, and a TRACKED file is audited wherever it sits.
SCRATCH_ROOTS = ("draft", "_site", ".git")

#: Scratch that appears at ANY depth. Running the bundled gate inside a deal
#: repo writes `.ci/reporting/bovsite/__pycache__/`, so a root-anchored match
#: would report interpreter cache as a confidentiality finding. `.gitignore`
#: matches these at any level too, which is what keeps the two paths agreeing.
SCRATCH_COMPONENTS = frozenset({"__pycache__", "node_modules"})

GITIGNORE_BODY = """# Generated by build_bov_site.py. Do not hand-edit.
# Local build scratch must never reach the public deploy repository.
draft/
_site/
node_modules/
__pycache__/
*.pyc
"""

SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def public_approval_record(record: dict[str, Any]) -> dict[str, Any]:
    """Project an internal approval record down to its public binding.

    Everything kept is evidence the server-side gate reads back:
    ``check_site_record`` compares the domain hashes, the approved price value,
    the scope, the approver, and the legacy flag. Everything dropped is the
    record of HOW the approval was reached, which is deliberation, not site
    data.

    Dropped, deliberately and by name: ``approval_note`` (the approver's
    verbatim words), ``channel``, ``price.confidence`` (an internal judgement,
    banned from client-facing surfaces by the pricing doctrine and by site-gate
    check 11b), ``price.range_low``/``range_high``/``basis_ref``, ``exceptions``
    (their descriptions are the reasoning behind media and diligence rulings),
    ``artifacts`` (internal review-package paths), and ``versions``.

    ``approved_exception_ids`` survives as bare identifiers because the media
    publication gate needs to know WHETHER an exception was approved for a
    stock or illustrative image. An id is an opaque handle; the description that
    explains the ruling is the deliberation and stays behind.
    """
    return {
        "schema_version": "1.0.0",
        "scope": record["scope"],
        "deal_id": record["deal_id"],
        "portfolio_id": record["portfolio_id"],
        "package_version": record["package_version"],
        "approved_at": record["approved_at"],
        "approver": record["approver"],
        "price": {"value": record["price"]["value"]},
        "approved_exception_ids": sorted(
            item["id"] for item in record["exceptions"] if item.get("approved")
        ),
        "domains": {
            name: {"content_sha256": value["content_sha256"]}
            for name, value in sorted(record["domains"].items())
        },
        "legacy_snapshot": record["legacy_snapshot"],
        "legacy_label": record["legacy_label"],
        "members": record["members"],
    }


def _git(site: Path, *arguments: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", str(site), *arguments],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return None


def _is_repo_root(site: Path) -> bool:
    """A deal repo nested inside an unrelated work tree is NOT its own repository.

    Answering from the parent's index would audit the wrong tree entirely, so
    that case falls back to a filesystem walk.
    """
    top = _git(site, "rev-parse", "--show-toplevel")
    return bool(top and top.strip() and Path(top.strip()).resolve() == Path(site).resolve())


def _walk(site: Path) -> list[str]:
    """Filesystem fallback for a tree git cannot answer for.

    Scratch trees are skipped only here, because there is no ignore machinery to
    consult. On a real repository git decides, and a TRACKED file is audited
    wherever it sits.
    """
    result = []
    for path in site.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(site).parts
        if parts[0] in SCRATCH_ROOTS or SCRATCH_COMPONENTS.intersection(parts):
            continue
        if path.suffix == ".pyc":
            continue
        result.append(path.relative_to(site).as_posix())
    return sorted(result)


def candidate_files(site: Path) -> list[str]:
    """Every repo-relative path that would be published if this tree shipped.

    Tracked files are audited WITHOUT a scratch exemption. `raw.githubusercontent`
    serves a tracked file no matter which directory it sits in, and no matter
    what `.gitignore` says: a `git add -f draft/notes.json` is published. The
    earlier version filtered scratch trees out of the tracked list too, which
    left exactly that hole (Codex P1, 2026-08-08).

    Untracked files come from `--exclude-standard`, so anything the generated
    `.gitignore` covers is already absent and no second filter is needed.
    """
    site = Path(site)
    if not _is_repo_root(site):
        return _walk(site)
    tracked = _git(site, "ls-files", "--cached")
    untracked = _git(site, "ls-files", "--others", "--exclude-standard")
    if tracked is None or untracked is None:
        return _walk(site)
    candidates = {
        line.strip() for line in (tracked + untracked).splitlines() if line.strip()
    }
    # An index entry whose worktree file is gone is a PENDING DELETION, not a
    # file this tree would publish. Auditing it made check 19 refuse the very
    # rebuild that removes an exposed record, so the documented remediation
    # failed on the repositories that needed it most (Codex P1, PR #109 round 4).
    # Tracked files that still exist are audited exactly as before.
    return sorted(path for path in candidates if (site / path).exists())


def purge_internal_record(site: Path) -> bool:
    """Remove the internal approval record from a deploy tree AND stage the removal.

    Every deal repository built before 2026-08-08 tracks one. Deleting only the
    worktree file is not a remediation: the path stays in the index, so the
    record stays published until somebody happens to commit that deletion. This
    stages it, so the operator's next commit actually removes it.

    Called from the BUILD path, not only from the approval writer. It used to
    run solely inside `write_deal_record`, which means `bov approve`, so the
    documented "just rebuild it" remediation did not remove anything at all
    (found while reproducing Codex P1, PR #109 round 4). Returns whether a file
    was removed.

    Git is best-effort: a tree that is not a repository, or a machine with no
    git, still gets the file deleted and never fails the build over staging.
    """
    site = Path(site)
    stale = site / INTERNAL_APPROVAL_FILENAME
    if not stale.is_file():
        return False
    stale.unlink()
    if _is_repo_root(site):
        # `git add` on a deleted path stages the deletion. Scoped to this one
        # path: the build never stages anything else on the operator's behalf.
        _git(site, "add", "--", INTERNAL_APPROVAL_FILENAME)
    return True


def generated_paths(site: Path) -> set[str] | None:
    """The scaffold paths this build wrote, or None when the manifest is absent.

    A tree with no scaffold at all needs no manifest: there is nothing for it to
    distinguish, and the empty set is the correct answer. The manifest becomes
    load-bearing exactly when `.ci/` or `.github/` exists, which is when a
    prefix allowance would otherwise have to be trusted.
    """
    site = Path(site)
    manifest = site / GENERATED_MANIFEST
    if not manifest.is_file():
        if not (site / ".ci").is_dir() and not (site / ".github").is_dir():
            return set()
        return None
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    paths = document.get("generated")
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        return None
    return set(paths)


def _allowed(path: str, property_slugs: set[str], generated: set[str]) -> bool:
    if path in SITE_ROOT_FILES or path == GENERATED_MANIFEST:
        return True
    if path.startswith("images/"):
        return Path(path).suffix.lower() in IMAGE_SUFFIXES
    # BOTH, never either. The grammar bounds what a scaffold path can be; the
    # manifest bounds which of those this build actually wrote.
    if is_scaffold_path(path) and path in generated:
        return True
    head, _, tail = path.partition("/")
    if tail == "index.html" and SLUG.match(head) and head in property_slugs:
        return True
    return False


def unexpected_files(site: Path, property_slugs: Iterable[str] = ()) -> list[str]:
    """Return every candidate path that is not a declared site artifact.

    `property_slugs` comes from the built payload, so a route directory is
    allowed only when the manifest actually declares that property. An
    undeclared `whatever/index.html` is refused rather than assumed to be a
    route. Scaffold files are allowed only when the build's own manifest names
    them; an absent manifest allows nothing, and the caller reports that once.
    """
    generated = generated_paths(site) or set()
    slugs = set(property_slugs)
    return [
        path for path in candidate_files(site)
        if not _allowed(path, slugs, generated)
    ]


def artifact_scope_errors(site: Path, property_slugs: Iterable[str] = ()) -> list[str]:
    """Human-readable refusals for a deploy tree that carries more than the site.

    Every message names the CLASS of artifact, never the content. This gate does
    not read a file and decide whether its strings are sensitive; it decides
    whether the file is part of the website.
    """
    if generated_paths(Path(site)) is None:
        return [
            f"{GENERATED_MANIFEST} is missing or unreadable, so the generated scaffold "
            f"cannot be distinguished from a hand-added file and the artifact-scope "
            f"allowlist cannot be applied. This tree was built by a generator older than "
            f"2026-08-08. Rebuild it with scripts/bov.py build."
        ]
    generated = generated_paths(Path(site)) or set()
    errors = []
    for path in unexpected_files(site, property_slugs):
        if Path(path).name == INTERNAL_APPROVAL_FILENAME:
            errors.append(
                f"{path} is the INTERNAL approval record, a deliberation artifact, and a "
                f"deploy repository is public. Delete it; the public binding is "
                f"{SITE_APPROVAL_FILENAME}."
            )
        elif path in generated:
            errors.append(
                f"{path} is listed in {GENERATED_MANIFEST} but is not shaped like anything "
                f"the build's scaffold writes. A manifest can only NARROW the allowlist, "
                f"never widen it, so listing a file does not authorize it."
            )
        else:
            errors.append(
                f"{path} is not a site artifact. A deal repository ships index.html, the "
                f"declared route pages, images/, CNAME, .nojekyll, og.jpg, the generated "
                f"data and binding files, and exactly the scaffold files named in "
                f"{GENERATED_MANIFEST}. Everything else stays in the workspace."
            )
    return errors


def preview_binding_paths(site: Path, property_slugs: Iterable[str] = ()) -> list[Path]:
    """Every existing site artifact the preview binding must hash.

    Derived from the same allowlist the scope gate enforces, so a new deploy
    artifact cannot be added to one and forgotten in the other.
    """
    site = Path(site)
    paths = [site / name for name in sorted(SITE_ROOT_FILES) if (site / name).is_file()]
    for slug in sorted(set(property_slugs)):
        route = site / slug / "index.html"
        if route.is_file():
            paths.append(route)
    images = site / "images"
    if images.is_dir():
        paths.extend(sorted(path for path in images.rglob("*") if path.is_file()))
    return paths
