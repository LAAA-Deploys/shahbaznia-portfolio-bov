"""Shared approval-record writer/gate mechanics for BOV workspaces."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any

from reporting.bovsite.contracts.canonical import canonical_sha256, file_sha256
from reporting.bovsite.contracts.domains import (
    DOMAIN_NAMES,
    domain_hashes,
    media_selection_projection,
    portfolio_domain_hashes,
)
from reporting.bovsite.contracts.loader import load_document, validate_document, write_json_atomic
from reporting.bovsite.contracts.workspace import DealWorkspace
from reporting.bovsite.copy_compile import publication_errors, support_reference_errors
from reporting.bovsite.payload import presentation_hashes
from reporting.bovsite.site_artifacts import (
    SITE_APPROVAL_FILENAME,
    public_approval_record,
    purge_internal_record,
)


#: Either partner may approve any LAAA deal, including the other's (Hard Rule 15;
#: Glen ruling 2026-08-19). This was the single name "Glen Scher" until
#: 2026-08-20. That one constant did BOTH halves of the damage: the record
#: builders wrote it in unconditionally, so a Filip approval was published to a
#: seller-facing site as Glen's, and the three validators rejected any other
#: name, so recording him honestly failed the build instead. The approver is now
#: passed in and checked against this tuple. Codex P1 on PR #196.
#:
#: Logan Ward added 2026-08-25 (Glen). Glen retired the teammate BOV review gate
#: on 2026-08-12: a teammate publishes their own BOV websites end to end, with no
#: per-deal approval from either partner. This tuple was the last place that
#: ruling was still untrue, and it fails CLOSED, so the sanctioned generator was
#: the one route Logan could not finish alone. 233-239 S Grand shipped
#: hand-built around it on 2026-08-25, which is the exact outcome the Camarillo
#: lock exists to prevent. Membership is deliberately flat rather than scoped
#: per deal: the record names who ACTUALLY approved and that name is published,
#: so attribution stays auditable without a deal-ownership model nothing else
#: here has.
#:
#: Morgan Wetmore added 2026-08-25 in the same session (Glen), on a Codex P1: the
#: teammate prompt this shipped with addresses "any LAAA teammate" and tells each one
#: to pass their own name, so a one-name allowlist promised a workflow it still
#: refused. Membership tracks who can actually RUN the workflow, which means both
#: LAAA-AI-Prompts push access and LAAA-Deploys org membership; add the rest as they
#: get access rather than pre-listing names that cannot reach the build.
#:
#: KEYED BY GITHUB LOGIN since 2026-08-28 (Glen). `bov approve` now DERIVES the
#: approver from the authenticated `gh` identity when `--approved-by` is omitted,
#: so this is the one place a person is registered: one entry, not a name here
#: plus a login-to-name map somewhere else that drifts out of step with it.
#: `--approved-by` still wins when passed, which is how a partner relays the
#: other's approval from their own machine. This SUPERSEDES the default-to-Glen
#: mechanism and preserves its zero-friction intent (Glen, 2026-08-28): the
#: default made an omitted flag publish somebody else's ruling under Glen's name,
#: silently, because Glen is allowlisted. Derivation cannot do that, and nobody
#: types anything.
APPROVERS = {
    "gscher1311": "Glen Scher",
    "fniculete-creator": "Filip Niculete",
    "Loganward-07": "Logan Ward",
    "morganwetmore-prog": "Morgan Wetmore",
}

#: The names, for membership checks and error messages. APPROVERS is keyed by
#: login; an approval record carries the NAME, so every check tests against this.
APPROVER_NAMES = tuple(APPROVERS.values())


def resolve_approver(explicit: str | None = None) -> str:
    """The name to publish as the approver.

    An explicit `--approved-by` always wins: that is a partner relaying the
    other's approval from their own machine, and it is the only case where the
    operator is not the approver.

    Otherwise derive it from the authenticated `gh` identity. Every machine that
    can run this workflow already has one, because publishing needs it, so this
    costs the operator nothing and cannot name the wrong person. An unmapped or
    unresolvable login is a loud error naming the flag, never a fallback to
    somebody else's name.
    """
    if explicit:
        return explicit
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApprovalError(
            "could not run `gh api user` to identify the approver "
            f"({exc}). Pass --approved-by \"<your full name>\" explicitly.") from exc
    login = result.stdout.strip()
    if result.returncode != 0 or not login:
        raise ApprovalError(
            "`gh api user` did not return a login, so the approver could not be "
            "identified. Authenticate with `gh auth login`, or pass "
            "--approved-by \"<your full name>\" explicitly. "
            f"gh said: {result.stderr.strip() or 'no output'}")
    if login not in APPROVERS:
        # Deliberately NO "or pass --approved-by instead" here, unlike the two failures
        # above. Those are a registered approver whose tooling failed, so naming
        # themselves is legitimate. This one KNOWS the login is not an approver, and an
        # agent told to pass a registered name would take the explicit path and publish a
        # valid seller-facing approval attributed to someone who never approved: exactly
        # the footgun the derivation removes, reintroduced in its own error message
        # (Codex P1, PR #265). The only exit is registering the login.
        raise ApprovalError(
            f"GitHub login {login!r} is not a registered approver, so this machine "
            "cannot record an approval. The approver is PUBLISHED in the site's "
            "approval record and must name whoever actually approved. Ask Glen to add "
            f"this line to APPROVERS in approval.py:\n"
            f"    \"{login}\": \"<Your Full Name>\",\n"
            "Do not approve under another person's name.")
    return APPROVERS[login]
LEGACY_LABEL = "NOT A RETROACTIVE APPROVAL"
REPO_ROOT = Path(__file__).resolve().parents[2]


class ApprovalError(ValueError):
    pass


def package_directory(workspace: DealWorkspace, version: str | None = None) -> tuple[str, Path, dict]:
    if version is None:
        pointer_path = workspace.review_root / "current.json"
        if not pointer_path.is_file():
            raise ApprovalError("review-package/current.json is missing")
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        version = pointer.get("package_version")
    if not isinstance(version, str) or not version.startswith("v") or not version[1:].isdigit():
        raise ApprovalError(f"invalid review package version: {version!r}")
    directory = workspace.review_root / version
    manifest_path = directory / "package-manifest.json"
    if not directory.is_dir() or not manifest_path.is_file():
        raise ApprovalError(f"immutable review package is missing: {directory}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("package_version") != version:
        raise ApprovalError("package manifest version does not match directory")
    return version, directory, manifest


def _git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _doctrine_version(path: Path) -> str:
    return f"sha256:{file_sha256(path)}" if path.is_file() else "not-present"


def exception_contract_errors(
    exceptions: dict, *, exact_price: float | None = None
) -> list[str]:
    errors = []
    seen: set[str] = set()
    for item in exceptions["items"]:
        if item["id"] in seen:
            errors.append(f"{item['id']}: duplicate exception id")
        seen.add(item["id"])
        value_effect = item.get("value_effect")
        if (
            item["materiality"] == "immaterial"
            and isinstance(value_effect, (int, float))
            and (
                abs(value_effect) > 100_000
                or (exact_price and abs(value_effect) / exact_price > 0.02)
            )
        ):
            errors.append(
                f"{item['id']}: value effect exceeds the material escalation threshold"
            )
        expected_domains = {
            "identity": "identity",
            "regulatory": "regulatory_conclusions",
            "no_safe_hero": "media_selection",
        }
        expected_domain = expected_domains.get(item["materiality"])
        if expected_domain and item["domain"] != expected_domain:
            errors.append(
                f"{item['id']}: {item['materiality']} materiality belongs to {expected_domain}"
            )
        if item["approved"] and item["status"] != "resolved":
            errors.append(f"{item['id']}: only resolved exceptions can be approved")
        if item["approved"] and not item["approval_note"]:
            errors.append(f"{item['id']}: approved exception needs an approval note")
    return errors


def unresolved_exception_errors(
    exceptions: dict, *, exact_price: float | None = None
) -> list[str]:
    errors = exception_contract_errors(exceptions, exact_price=exact_price)
    for item in exceptions["items"]:
        if item["status"] in {"open", "escalated"}:
            errors.append(f"{item['id']}: exception remains {item['status']}")
        if item["materiality"] != "immaterial" and item["status"] == "resolved" and not item["approved"]:
            errors.append(f"{item['id']}: material exception is resolved but not approved")
    return errors


def deal_exception_summary(exceptions: dict) -> list[dict[str, Any]]:
    return sorted(
        [
        {
            "id": item["id"],
            "description": item["description"],
            "domain": item["domain"],
            "approved": item["approved"],
        }
        for item in exceptions["items"]
        ],
        key=lambda item: item["id"],
    )


def portfolio_exception_summary(members: dict[str, DealWorkspace]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for deal_id, member in members.items():
        for item in member.load("exceptions")["items"]:
            result.append(
                {
                    "id": f"{deal_id}:{item['id']}",
                    "description": item["description"],
                    "domain": item["domain"],
                    "approved": item["approved"],
                }
            )
    return sorted(result, key=lambda item: item["id"])


def deal_price_summary(pricing: dict) -> dict[str, Any]:
    decision = pricing["decision"]
    return {
        "value": decision["exact_price"],
        "range_low": decision["range_low"],
        "range_high": decision["range_high"],
        "confidence": decision["confidence"],
        "basis_ref": "pricing.json#/decision",
    }


def artifact_audit(package_dir: Path, artifacts: dict) -> list[str]:
    """Return audit-only mismatches.  These never decide content approval validity."""
    warnings: list[str] = []

    def inspect(label: str, entry: Any) -> None:
        if not isinstance(entry, dict):
            warnings.append(f"{label}: artifact entry is malformed")
            return
        raw_path, expected = entry.get("path"), entry.get("sha256")
        if not isinstance(raw_path, str) or not isinstance(expected, str):
            warnings.append(f"{label}: artifact path/hash is malformed")
            return
        candidate = (package_dir / raw_path).resolve()
        try:
            candidate.relative_to(package_dir.resolve())
        except ValueError:
            warnings.append(f"{label}: artifact path escapes package")
            return
        if not candidate.is_file():
            warnings.append(f"{label}: artifact is missing: {raw_path}")
        elif file_sha256(candidate) != expected:
            warnings.append(f"{label}: artifact SHA-256 differs from the approval audit record")

    for label, entry in artifacts.items():
        if isinstance(entry, list):
            for index, item in enumerate(entry):
                inspect(f"{label}[{index}]", item)
        else:
            inspect(label, entry)
    return warnings


def _media_selection_core(projection: dict[str, Any]) -> dict[str, Any]:
    return {
        key: projection.get(key)
        for key in ("deal_id", "order", "hero", "items")
    }


def _portfolio_media_projection(
    media: dict[str, Any], deal_id: str, property_payload: dict[str, Any]
) -> dict[str, Any]:
    """Reconstruct one member's approved selection from the combined site manifest."""
    prefix = f"{deal_id}:"
    combined_ids = [item_id for item_id in media["published"]["order"] if item_id.startswith(prefix)]
    combined_by_id = {item["id"]: item for item in media["archive"]}
    archive = []
    hero = None
    rendered_hero = property_payload.get("hero")
    for combined_id in combined_ids:
        item = combined_by_id.get(combined_id)
        if item is None:
            continue
        restored = dict(item)
        restored["id"] = combined_id[len(prefix) :]
        exception_id = restored.get("exception_id")
        if isinstance(exception_id, str) and exception_id.startswith(prefix):
            restored["exception_id"] = exception_id[len(prefix) :]
        archive.append(restored)
        if rendered_hero == f"images/{item['filename']}":
            hero = restored["id"]
    manifest = {
        "deal_id": deal_id,
        "archive": archive,
        "published": {
            "order": [combined_id[len(prefix) :] for combined_id in combined_ids],
            "hero": hero,
        },
    }
    return media_selection_projection(manifest)


def build_deal_record(
    workspace: DealWorkspace,
    *,
    approver: str,
    approval_note: str,
    channel: str,
    package_version: str | None = None,
    approved_at: str | None = None,
    legacy_snapshot: bool = False,
) -> dict:
    if not approval_note.strip():
        raise ApprovalError("approval note must quote the approver's approval verbatim")
    if approver not in APPROVER_NAMES:
        raise ApprovalError(
            "approver must be one of " + ", ".join(repr(n) for n in APPROVER_NAMES)
            + f", got {approver!r}. The approver is PUBLISHED in the site's "
            "approval record, so it must name whoever actually approved.")
    deal = workspace.load("deal")
    pricing = workspace.load("pricing")
    copy = workspace.load("copy")
    exceptions = workspace.load("exceptions")
    problems = [
        *publication_errors(copy),
        *support_reference_errors(copy, workspace),
        *unresolved_exception_errors(
            exceptions, exact_price=pricing["decision"]["exact_price"]
        ),
    ]
    if problems:
        raise ApprovalError("approval prerequisites failed: " + "; ".join(problems))
    current_hashes = domain_hashes(workspace)
    version, package_dir, manifest = package_directory(workspace, package_version)
    if manifest.get("deal_id") != deal["deal_id"]:
        raise ApprovalError("review package deal_id does not match deal.json")
    if manifest.get("domain_hashes_at_generation") != current_hashes:
        raise ApprovalError(
            "spine content changed after the review package was generated; build a new immutable version"
        )
    record = {
        "schema_version": "1.0.0",
        "scope": "deal",
        "deal_id": deal["deal_id"],
        "portfolio_id": None,
        "package_version": version,
        "approved_at": approved_at or datetime.now(timezone.utc).isoformat(),
        "approver": approver,
        "channel": channel,
        "approval_note": approval_note,
        "price": deal_price_summary(pricing),
        "domains": {
            name: {"content_sha256": current_hashes[name]} for name in DOMAIN_NAMES
        },
        "artifacts": manifest["artifacts"],
        "exceptions": deal_exception_summary(exceptions),
        "versions": {
            "generator_git_sha": _git_sha(),
            "comp_analysis": _doctrine_version(REPO_ROOT / "comps" / "comp_analysis.md"),
            "bov_workflow": _doctrine_version(REPO_ROOT / "reporting" / "bov_workflow.md"),
        },
        "legacy_snapshot": legacy_snapshot,
        "legacy_label": LEGACY_LABEL if legacy_snapshot else None,
        "members": None,
    }
    validate_document("approval-record", record)
    return record


def write_deal_record(workspace: DealWorkspace, record: dict) -> tuple[Path, Path]:
    """Write the full record to the workspace and its public binding to the site.

    The two files are different documents on purpose. A deal repository is
    public, so it receives hash-only binding evidence under its own name and
    schema; the deliberation record (approval note, confidence, exception
    reasoning, artifact paths) never leaves the workspace.
    """
    site_repo = workspace.site_repo
    if not site_repo.is_dir():
        raise ApprovalError(f"configured site repo does not exist: {site_repo}")
    workspace_path = workspace.data_dir / "approval-record.json"
    site_path = site_repo / SITE_APPROVAL_FILENAME
    binding = public_approval_record(record)
    validate_document("site-approval", binding)
    write_json_atomic(workspace_path, record)
    write_json_atomic(site_path, binding)
    purge_internal_record(site_repo)
    return workspace_path, site_path




def check_deal_record(workspace: DealWorkspace, record_path: Path | None = None) -> tuple[list[str], list[str], dict | None]:
    errors: list[str] = []
    warnings: list[str] = []
    path = record_path or workspace.data_dir / "approval-record.json"
    try:
        record = load_document(path, "approval-record")
    except Exception as exc:
        return [str(exc)], warnings, None
    if record["scope"] != "deal":
        errors.append("approval scope is not deal")
    deal = workspace.load("deal")
    if record["deal_id"] != deal["deal_id"]:
        errors.append("approval deal_id does not match deal.json")
    if record["approver"] not in APPROVER_NAMES:
        errors.append("approval approver must be one of "
                      + ", ".join(repr(name) for name in APPROVER_NAMES))
    if record["legacy_snapshot"] and record.get("legacy_label") != LEGACY_LABEL:
        errors.append(f"legacy snapshots must be labelled {LEGACY_LABEL!r}")
    if record["legacy_snapshot"]:
        errors.append("legacy snapshot is not a publication approval")
    if not record["legacy_snapshot"] and record.get("legacy_label") is not None:
        errors.append("non-legacy approvals cannot carry a legacy label")
    current = domain_hashes(workspace)
    for name in DOMAIN_NAMES:
        if record["domains"][name]["content_sha256"] != current[name]:
            errors.append(f"domain invalid: {name}")
    copy = workspace.load("copy")
    exceptions = workspace.load("exceptions")
    pricing = workspace.load("pricing")
    errors.extend(publication_errors(copy))
    errors.extend(support_reference_errors(copy, workspace))
    errors.extend(
        unresolved_exception_errors(
            exceptions, exact_price=pricing["decision"]["exact_price"]
        )
    )
    if record["price"] != deal_price_summary(pricing):
        errors.append("approval price summary differs from current pricing decision")
    if record["exceptions"] != deal_exception_summary(exceptions):
        errors.append("approval exception summary differs from current exceptions")
    try:
        version, package_dir, manifest = package_directory(workspace, record["package_version"])
        if version != record["package_version"]:
            errors.append("approval package version mismatch")
        if manifest.get("deal_id") != deal["deal_id"]:
            errors.append("approval package deal_id differs from current deal")
        if manifest.get("domain_hashes_at_generation") != current:
            errors.append("approval package content hashes differ from current approved domains")
        warnings.extend(artifact_audit(package_dir, record["artifacts"]))
    except ApprovalError as exc:
        errors.append(str(exc))
    return errors, warnings, record


def check_site_record(site: Path) -> tuple[list[str], list[str], dict | None]:
    """Check generated public artifacts without requiring the private workspace.

    Reads the PUBLIC binding. `approval-record.json` is a workspace document and
    is not consulted here even if one is present, so this gate can never be
    satisfied by a deliberation artifact that should not be in the tree; the
    artifact-scope gate in check_bov_site.py refuses that tree separately.
    """
    errors: list[str] = []
    warnings: list[str] = []
    try:
        record = load_document(site / SITE_APPROVAL_FILENAME, "site-approval")
        payload = json.loads((site / "bov-site.json").read_text(encoding="utf-8"))
        media = load_document(site / "media-manifest.json", "media-manifest")
    except Exception as exc:
        return [str(exc)], warnings, None
    binding = payload.get("approval_binding")
    if not isinstance(binding, dict):
        return ["bov-site.json has no generated approval_binding"], warnings, record
    if record["scope"] not in {"deal", "portfolio"}:
        errors.append("site approval scope is unsupported")
    if record["legacy_snapshot"]:
        errors.append("legacy snapshot is not a publication approval")
    if binding.get("deal_id") != record["deal_id"] or media["deal_id"] != record["deal_id"]:
        errors.append("site artifacts do not share the approval deal_id")
    bound_domains = binding.get("domains")
    if not isinstance(bound_domains, dict):
        errors.append("approval_binding.domains is missing")
    else:
        for name in DOMAIN_NAMES:
            if bound_domains.get(name) != record["domains"][name]["content_sha256"]:
                errors.append(f"site binding differs from approved domain: {name}")
    expected_presentation = presentation_hashes(payload)
    if binding.get("presentation_hashes") != expected_presentation:
        errors.append("generated presentation fields changed after the approval-bound build")

    properties = payload.get("properties") or []
    if record["scope"] == "deal":
        copy_contract = (payload.get("copy_contract") or {}).get("document")
        if not isinstance(copy_contract, dict):
            errors.append("site payload lacks its canonical copy contract")
        else:
            copy_hash = canonical_sha256(copy_contract)
            if copy_hash != record["domains"]["copy"]["content_sha256"]:
                errors.append("site copy does not match the approved copy domain")
            errors.extend(publication_errors(copy_contract))
        approval_contract = payload.get("approval_contract") or {}
        media_contract = approval_contract.get("media_selection")
        if not isinstance(media_contract, dict):
            errors.append("site payload lacks its canonical media-selection contract")
        else:
            if canonical_sha256(media_contract) != record["domains"]["media_selection"]["content_sha256"]:
                errors.append("site media contract does not match the approved media-selection domain")
            if _media_selection_core(media_contract) != media_selection_projection(media):
                errors.append("site media manifest differs from its canonical approved selection")
        regulatory_contract = approval_contract.get("regulatory_conclusions")
        if not isinstance(regulatory_contract, dict):
            errors.append("site payload lacks its canonical regulatory contract")
        else:
            if canonical_sha256(regulatory_contract) != record["domains"]["regulatory_conclusions"]["content_sha256"]:
                errors.append("site regulatory contract does not match approval")
            if regulatory_contract.get("conclusions") != payload.get("regulatory_conclusions"):
                errors.append("site regulatory conclusions differ from the approved contract")
        if len(properties) != 1:
            errors.append("deal-scoped site approval requires exactly one property")
        elif properties[0].get("price") != record["price"]["value"]:
            errors.append("site price differs from the approved exact price")
        if properties:
            expected_hero = media["published"]["hero"]
            by_id = {item["id"]: item for item in media["archive"]}
            if expected_hero:
                item = by_id.get(expected_hero)
                expected_ref = f"images/{item['filename']}" if item else None
            else:
                expected_ref = (media_contract.get("presentation") or {}).get(
                    "no_photo_cover"
                ) if isinstance(media_contract, dict) else None
            if properties[0].get("hero") != expected_ref:
                errors.append("site hero differs from the approved media selection")
            expected_gallery = [
                {
                    "src": f"images/{by_id[item_id]['filename']}",
                    "alt": by_id[item_id]["category"].replace("_", " ").title(),
                }
                for item_id in media["published"]["order"]
                if item_id != expected_hero and item_id in by_id
            ]
            if properties[0].get("gallery") != expected_gallery:
                errors.append("site gallery differs from the approved media order")
    else:
        if payload.get("site_mode") != "portfolio" or len(properties) < 2:
            errors.append("portfolio approval requires a multi-property portfolio payload")
        if binding.get("members") != record.get("members"):
            errors.append("site portfolio member binding differs from approval")
        contracts = ((payload.get("portfolio_contract") or {}).get("members") or {})
        properties_by_deal = {
            prop.get("deal_id"): prop for prop in properties if isinstance(prop, dict)
        }
        for deal_id, hashes in (record.get("members") or {}).items():
            contract = contracts.get(deal_id)
            if not isinstance(contract, dict):
                errors.append(f"site lacks canonical member contract for {deal_id}")
                continue
            for domain, key in (
                ("copy", "copy"),
                ("media_selection", "media_selection"),
                ("regulatory_conclusions", "regulatory_conclusions"),
            ):
                if canonical_sha256(contract.get(key)) != hashes[domain]:
                    errors.append(f"site {deal_id} {domain} contract differs from approval")
            errors.extend(
                f"{deal_id}: {error}"
                for error in publication_errors(contract.get("copy") or {})
            )
            member_property = properties_by_deal.get(deal_id)
            if not isinstance(member_property, dict):
                errors.append(f"site lacks property payload for approved member {deal_id}")
            else:
                actual_media = _portfolio_media_projection(media, deal_id, member_property)
                if _media_selection_core(contract.get("media_selection") or {}) != actual_media:
                    errors.append(f"site {deal_id} combined media differs from approval")
                member_media = contract.get("media_selection") or {}
                prefix = f"{deal_id}:"
                combined_by_id = {item["id"]: item for item in media["archive"]}
                member_hero = member_media.get("hero")
                combined_hero = f"{prefix}{member_hero}" if member_hero else None
                hero_item = combined_by_id.get(combined_hero) if combined_hero else None
                expected_member_hero = (
                    f"images/{hero_item['filename']}"
                    if hero_item
                    else (member_media.get("presentation") or {}).get("no_photo_cover")
                )
                if member_property.get("hero") != expected_member_hero:
                    errors.append(f"site {deal_id} hero differs from approval")
                expected_member_gallery = []
                for raw_id in member_media.get("order") or []:
                    if raw_id == member_hero:
                        continue
                    item = combined_by_id.get(f"{prefix}{raw_id}")
                    if item:
                        expected_member_gallery.append(
                            {
                                "src": f"images/{item['filename']}",
                                "alt": item["category"].replace("_", " ").title(),
                            }
                        )
                if member_property.get("gallery") != expected_member_gallery:
                    errors.append(f"site {deal_id} gallery differs from approval")
                visible_regulatory = (payload.get("regulatory_conclusions") or {}).get(deal_id)
                if (contract.get("regulatory_conclusions") or {}).get("conclusions") != visible_regulatory:
                    errors.append(f"site {deal_id} regulatory conclusions differ from approval")
        expected_hero = media["published"]["hero"]
        by_id = {item["id"]: item for item in media["archive"]}
        if expected_hero:
            item = by_id.get(expected_hero)
            expected_ref = f"images/{item['filename']}" if item else None
            rendered_ref = (payload.get("meta") or {}).get("hero")
            derived_portfolio_cover = isinstance(rendered_ref, str) and rendered_ref.startswith(
                "images/portfolio-hero-split"
            )
            if rendered_ref != expected_ref and not derived_portfolio_cover:
                errors.append("portfolio cover hero differs from published media manifest")
    if record["approver"] not in APPROVER_NAMES:
        errors.append("approval approver must be one of "
                      + ", ".join(repr(name) for name in APPROVER_NAMES))
    return errors, warnings, record


def portfolio_members(workspace: DealWorkspace) -> tuple[dict, dict[str, DealWorkspace]]:
    portfolio = workspace.load("portfolio")
    members: dict[str, DealWorkspace] = {}
    for member in portfolio["members"]:
        member_root = workspace.resolve_relative(
            member["workspace"], label=f"portfolio member {member['deal_id']}"
        )
        member_workspace = DealWorkspace.from_path(member_root)
        actual_id = member_workspace.load("deal")["deal_id"]
        if actual_id != member["deal_id"]:
            raise ApprovalError(
                f"portfolio member {member['deal_id']!r} points to deal {actual_id!r}"
            )
        if actual_id in members:
            raise ApprovalError(f"duplicate portfolio member {actual_id!r}")
        members[actual_id] = member_workspace
    return portfolio, members


def aggregate_portfolio_hashes(workspace: DealWorkspace) -> tuple[dict, dict[str, dict[str, str]], dict[str, str]]:
    portfolio, members = portfolio_members(workspace)
    member_hashes = {deal_id: domain_hashes(member) for deal_id, member in members.items()}
    return portfolio, member_hashes, portfolio_domain_hashes(portfolio, member_hashes)


def portfolio_site_repo(workspace: DealWorkspace, portfolio: dict | None = None) -> Path:
    portfolio = portfolio or workspace.load("portfolio")
    return workspace.resolve_relative(portfolio["site_repo"], label="portfolio.site_repo")


def build_portfolio_record(
    workspace: DealWorkspace,
    *,
    approver: str,
    approval_note: str,
    channel: str,
    package_version: str | None = None,
    approved_at: str | None = None,
) -> dict:
    if not approval_note.strip():
        raise ApprovalError("approval note must quote the approver's approval verbatim")
    if approver not in APPROVER_NAMES:
        raise ApprovalError(
            "approver must be one of " + ", ".join(repr(n) for n in APPROVER_NAMES)
            + f", got {approver!r}. The approver is PUBLISHED in the site's "
            "approval record, so it must name whoever actually approved.")
    portfolio, members = portfolio_members(workspace)
    member_hashes = {deal_id: domain_hashes(member) for deal_id, member in members.items()}
    aggregate = portfolio_domain_hashes(portfolio, member_hashes)
    problems: list[str] = []
    member_pricing: dict[str, dict] = {}
    for deal_id, member in members.items():
        member_copy = member.load("copy")
        problems.extend(f"{deal_id}: {item}" for item in publication_errors(member_copy))
        problems.extend(
            f"{deal_id}: {item}"
            for item in support_reference_errors(member_copy, member)
        )
        problems.extend(
            f"{deal_id}: {item}"
            for item in unresolved_exception_errors(
                member.load("exceptions"),
                exact_price=member.load("pricing")["decision"]["exact_price"],
            )
        )
        member_pricing[deal_id] = member.load("pricing")
    if problems:
        raise ApprovalError("portfolio approval prerequisites failed: " + "; ".join(problems))
    if portfolio["portfolio_price"] is None:
        raise ApprovalError("portfolio approval requires portfolio_price")
    version, _package_dir, manifest = package_directory(workspace, package_version)
    if manifest.get("portfolio_id") != portfolio["portfolio_id"]:
        raise ApprovalError("review package portfolio_id does not match portfolio.json")
    if manifest.get("domain_hashes_at_generation") != aggregate:
        raise ApprovalError(
            "portfolio content changed after the review package was generated; build a new version"
        )
    if manifest.get("member_hashes_at_generation") != member_hashes:
        raise ApprovalError("portfolio package member hashes do not match current members")
    range_low = sum(item["decision"]["range_low"] for item in member_pricing.values())
    range_high = sum(item["decision"]["range_high"] for item in member_pricing.values())
    confidence_rank = {"low": 0, "medium": 1, "high": 2}
    confidence = min(
        (item["decision"]["confidence"] for item in member_pricing.values()),
        key=confidence_rank.__getitem__,
    )
    record = {
        "schema_version": "1.0.0",
        "scope": "portfolio",
        "deal_id": portfolio["portfolio_id"],
        "portfolio_id": portfolio["portfolio_id"],
        "package_version": version,
        "approved_at": approved_at or datetime.now(timezone.utc).isoformat(),
        "approver": approver,
        "channel": channel,
        "approval_note": approval_note,
        "price": {
            "value": portfolio["portfolio_price"],
            "range_low": range_low,
            "range_high": range_high,
            "confidence": confidence,
            "basis_ref": "portfolio.json#/portfolio_price",
        },
        "domains": {name: {"content_sha256": aggregate[name]} for name in DOMAIN_NAMES},
        "artifacts": manifest["artifacts"],
        "exceptions": portfolio_exception_summary(members),
        "versions": {
            "generator_git_sha": _git_sha(),
            "comp_analysis": _doctrine_version(REPO_ROOT / "comps" / "comp_analysis.md"),
            "bov_workflow": _doctrine_version(REPO_ROOT / "reporting" / "bov_workflow.md"),
        },
        "legacy_snapshot": False,
        "legacy_label": None,
        "members": member_hashes,
    }
    validate_document("approval-record", record)
    return record


def write_portfolio_record(workspace: DealWorkspace, record: dict) -> tuple[Path, Path]:
    """Full record to the workspace, public binding to the portfolio site repo."""
    site = portfolio_site_repo(workspace)
    if not site.is_dir():
        raise ApprovalError(f"configured portfolio site repo does not exist: {site}")
    workspace_path = workspace.data_dir / "approval-record.json"
    site_path = site / SITE_APPROVAL_FILENAME
    binding = public_approval_record(record)
    validate_document("site-approval", binding)
    write_json_atomic(workspace_path, record)
    write_json_atomic(site_path, binding)
    purge_internal_record(site)
    return workspace_path, site_path


def check_portfolio_record(workspace: DealWorkspace) -> tuple[list[str], list[str], dict | None]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        record = load_document(workspace.data_dir / "approval-record.json", "approval-record")
        portfolio, member_hashes, aggregate = aggregate_portfolio_hashes(workspace)
    except Exception as exc:
        return [str(exc)], warnings, None
    if record["scope"] != "portfolio":
        errors.append("approval scope is not portfolio")
    if record.get("portfolio_id") != portfolio["portfolio_id"]:
        errors.append("approval portfolio_id does not match portfolio.json")
    if record["approver"] not in APPROVER_NAMES:
        errors.append("approval approver must be one of "
                      + ", ".join(repr(name) for name in APPROVER_NAMES))
    if record["legacy_snapshot"]:
        errors.append("portfolio approvals cannot be legacy snapshots")
    for domain in DOMAIN_NAMES:
        if record["domains"][domain]["content_sha256"] != aggregate[domain]:
            errors.append(f"portfolio domain invalid: {domain}")
    if record.get("members") != member_hashes:
        errors.append("approval member hash map differs from current portfolio members")
    _portfolio_again, current_members = portfolio_members(workspace)
    member_pricing: dict[str, dict] = {}
    prerequisite_errors: list[str] = []
    for deal_id, member in current_members.items():
        copy = member.load("copy")
        prerequisite_errors.extend(
            f"{deal_id}: {item}" for item in publication_errors(copy)
        )
        prerequisite_errors.extend(
            f"{deal_id}: {item}" for item in support_reference_errors(copy, member)
        )
        prerequisite_errors.extend(
            f"{deal_id}: {item}"
            for item in unresolved_exception_errors(
                member.load("exceptions"),
                exact_price=member.load("pricing")["decision"]["exact_price"],
            )
        )
        member_pricing[deal_id] = member.load("pricing")
    errors.extend(prerequisite_errors)
    confidence_rank = {"low": 0, "medium": 1, "high": 2}
    expected_price = {
        "value": portfolio["portfolio_price"],
        "range_low": sum(item["decision"]["range_low"] for item in member_pricing.values()),
        "range_high": sum(item["decision"]["range_high"] for item in member_pricing.values()),
        "confidence": min(
            (item["decision"]["confidence"] for item in member_pricing.values()),
            key=confidence_rank.__getitem__,
        ),
        "basis_ref": "portfolio.json#/portfolio_price",
    }
    if record["price"] != expected_price:
        errors.append("approval price summary differs from current portfolio decision")
    if record["exceptions"] != portfolio_exception_summary(current_members):
        errors.append("approval exception summary differs from current portfolio exceptions")
    try:
        _version, package_dir, manifest = package_directory(workspace, record["package_version"])
        if manifest.get("portfolio_id") != portfolio["portfolio_id"]:
            errors.append("approval package portfolio_id differs from current portfolio")
        if manifest.get("domain_hashes_at_generation") != aggregate:
            errors.append("approval package content hashes differ from current portfolio domains")
        if manifest.get("member_hashes_at_generation") != member_hashes:
            errors.append("approval package member hashes differ from current portfolio members")
        warnings.extend(artifact_audit(package_dir, record["artifacts"]))
    except Exception as exc:
        errors.append(str(exc))
    return errors, warnings, record
