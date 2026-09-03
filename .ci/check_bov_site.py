#!/usr/bin/env python3
"""The BOV website gate. Exit 0 to ship, exit 1 to stop.

    python LAAA-AI-Prompts/scripts/check_bov_site.py <deal-repo>

Run before the first commit and again before deploy. The same checks run
server-side on every push via the GitHub Action in
`reporting/bovsite/workflow/deploy-pages.yml`, which is the copy that cannot be
skipped.

Every check below is an actual failure, not a hypothetical:

  A BOV shipped as a Slidev deck (11025 Blix, 2026-05-27) and another as a
  React/Vite SPA (Brio, 2026-07-30). Both times a correct rule existed and the
  build path did not read it. Checks 1 and 2.

  A build shipped with no webfont link and roughly 2x the locked type scale,
  which is why "it looks nothing like Camarillo" is detectable as a string.
  Checks 3 and 4.

  Maps were rendered with Leaflet and OpenStreetMap, and subjects geocoded via
  the Census interpolator, which put a pin 33m into the street on 3837 College.
  Checks 6 and 7.

  A Google Maps API key was emitted into client HTML. Check 8.

  "Marcus &amp;amp; Millichap" reached production in three headings because a
  pre-escaped string was passed through the template escaper. Check 10.

  The internal audience segment name "Clients" reached client-facing copy.
  Check 11.

  A rebuild silently reverted the live track-record and marketing figures to a
  hardcoded snapshot that was stale and looked correct. Check 12.

  The internal approval record shipped into the PUBLIC deal repository and was
  readable, unauthenticated, at raw.githubusercontent.com. The deploy workflow
  stages an allowlist into _site, so the published site was never the defect;
  the repository was. Check 19, an allowlist over the whole deploy tree.

CALIBRATION MATTERS. Every rule here was run against the locked reference build
before being enforced. The price-before-reveal rule in particular has an
explicit carve-out because Camarillo itself quotes the price inside a buyer
objection answer in #property-info, and a naive version of that check would
permanently fail the very design it is protecting.
"""
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _site_artifacts():
    """Load the shared deploy-artifact allowlist from whichever layout we are in.

    This gate runs from three places and must behave identically in all three:
    the repo (`scripts/`), the `.ci/` bundle copied into a deal repo, and the
    laaa-core plugin (`plugins/laaa-core/scripts/`). Loading by explicit file
    path keeps the rule in ONE module without giving a standalone gate a package
    dependency or a sys.path trick. A missing module fails closed: a gate that
    silently skips its allowlist is how the leak survived in the first place.
    """
    here = Path(__file__).resolve().parent
    candidates = (
        here / "reporting" / "bovsite" / "site_artifacts.py",         # .ci/ bundle
        here.parent / "reporting" / "bovsite" / "site_artifacts.py",  # repo
        here.parent / "vendor" / "bovsite" / "site_artifacts.py",     # laaa-core plugin
    )
    for candidate in candidates:
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("bov_site_artifacts", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit(
        "site_artifacts.py is missing beside this gate, so the deploy-artifact "
        "allowlist cannot be enforced. Rebuild with build_bov_site.py, which "
        "refreshes .ci/ on every run.\n  looked in: "
        + "\n             ".join(str(path) for path in candidates)
    )


FRAMEWORK_CONFIGS = ["vite.config.ts", "vite.config.js", "next.config.js", "next.config.mjs",
                     "svelte.config.js", "astro.config.mjs", "nuxt.config.ts", "remix.config.js"]
SPA_MARKERS = [
    (r'<script[^>]+type=["\']module["\']', "ES module script (bundler output)"),
    (r'/assets/[A-Za-z0-9_-]+-[A-Za-z0-9]{6,}\.js', "hashed bundle reference"),
    (r'data-reactroot|__NEXT_DATA__|<div id=["\']root["\']></div>|id=["\']__nuxt["\']',
     "SPA hydration root"),
]
BANNED_SOURCES = [
    (r'leaflet', "Leaflet"),
    (r'openstreetmap|tile\.osm', "OpenStreetMap"),
    (r'unpkg\.com|cdnjs\.|jsdelivr', "third-party CDN"),
    (r'geocoding\.geo\.census\.gov', "Census geocoder"),
    (r'nominatim', "Nominatim"),
]
#: A build older than this is not "current" for a client-facing page. Seven
#: days (Glen, 2026-08-07): the original 24-hour window forced a re-approval of
#: an unchanged two-line diff when a 10-minute outage pushed a build past the
#: window (the 38050 Palmdale resume ledger, 2026-08-06/07). One week is long
#: enough to build, review, and release without a rebuild, short enough that a
#: figure cannot quietly drift for a quarter.
MAX_FIGURE_AGE_HOURS = 168

#: Validated-SHA exemption (Glen, 2026-08-07). When a release dispatch targets
#: a commit that ALREADY passed this gate, the age of its figures is a warning,
#: not a failure: re-publishing the exact approved bytes must not require a
#: rebuild that would change them. The exemption is SHA-bound and narrow:
#: `BOV_VALIDATED_SHA` must equal the site work tree's HEAD, and only the
#: figure-age rule is relaxed. Every other rule still fails the build.
VALIDATED_SHA_ENV = "BOV_VALIDATED_SHA"

REQUIRED_FILES = ["index.html", "CNAME", ".nojekyll", "og.jpg"]
SECTION_ORDER = ["track-record", "marketing", "active-listings", "property-info",
                 "investment", "location",
                 "prop-details", "photos", "rent-comps", "sale-comps",
                 "opinion-of-value", "on-market",
                 "financials", "contact"]

#: Every finding, as (message, declared_class). `declared_class` is None when
#: the raising check left classification to `report`, and a bool when the check
#: declared the class outright.
fails, warns = [], []


def fail(msg, blocking=None):
    """Record a failure, optionally DECLARING whether it blocks.

    Left as None, `report` infers the class by matching the message text
    against `_BLOCKING_PATTERNS`, which is how every check here still behaves.
    That inference is a guess about prose, and the guess has been wrong twice.
    The certified-map tamper class printed as advisory and the gate exited 0
    until "map-manifest" was added to the pattern list. Check 19's
    artifact-scope refusals printed as advisory for the same reason: none of
    their wording happens to contain a listed substring, so an internal
    approval record sitting in a PUBLIC deal repository was reported as a
    finding that creates no work.

    A check that already knows its class says so here. No later rewording of
    the message can lose it, and the pattern list stops being the only place a
    blocking class can be declared.
    """
    fails.append((msg, blocking))


def warn(msg):
    warns.append(msg)


def validated_sha_exemption(site):
    """Return the exempting SHA when BOV_VALIDATED_SHA matches the site HEAD.

    The claim is verified, never trusted: the environment variable must name
    the exact commit this work tree is at, or the exemption is ignored with a
    warning. A claim without a git work tree proves nothing and is ignored too.
    """
    claimed = os.environ.get(VALIDATED_SHA_ENV, "").strip()
    if not claimed:
        return None
    try:
        head = subprocess.run(
            ["git", "-C", str(site), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        warn(f"{VALIDATED_SHA_ENV} is set but {site} is not a git work tree; exemption ignored")
        return None
    if head != claimed:
        warn(f"{VALIDATED_SHA_ENV} {claimed[:12]} does not match site HEAD {head[:12]}; exemption ignored")
        return None
    return claimed


#: Build scratch, not deployable routes. Auditing _site double-counts the
#: staged copy of the same pages and reports a route that does not exist.
NOT_ROUTES = {"_site", "node_modules", "raw", "images", "template", ".git", ".github", "__pycache__"}


def routes(site):
    out = []
    if (site / "index.html").exists():
        out.append(site / "index.html")
    for d in sorted(p for p in site.iterdir() if p.is_dir()):
        if d.name in NOT_ROUTES or d.name.startswith("."):
            continue
        if (d / "index.html").exists():
            out.append(d / "index.html")
    return out


def image_references(html):
    """Return image assets from ordinary attributes, CSS URLs, and srcset.

    ``srcset`` is its own URL-list grammar. Treating it as ``src`` silently
    skipped the mobile side of every ``<picture>`` element, so a missing tall
    map could pass while the desktop image existed.
    """
    suffix = r"(?:jpg|jpeg|png|webp|svg)"
    refs = set(re.findall(
        rf"""(?:src|href|content)=["']([^"']+\.{suffix}(?:[?#][^"']*)?)["']""",
        html,
        re.I,
    ))
    for value in re.findall(r"""srcset=["']([^"']+)["']""", html, re.I):
        for candidate in value.split(","):
            ref = candidate.strip().split()[0] if candidate.strip() else ""
            if re.search(rf"\.{suffix}(?:[?#].*)?$", ref, re.I):
                refs.add(ref)
    for groups in re.findall(
            rf"""url\(\s*(?:"([^"]+\.{suffix}(?:[?#][^"]*)?)"|'([^']+\.{suffix}(?:[?#][^']*)?)'|([^)"']+\.{suffix}(?:[?#][^)"']*)?))\s*\)""",
            html,
            re.I):
        refs.add(next(group for group in groups if group))
    return refs


def local_image_path(site, page, ref, domain):
    """Resolve a page image reference when it belongs to this build."""
    if ref.startswith("data:"):
        return None
    if ref.startswith(("http://", "https://")):
        if not domain or domain not in ref:
            return None
        relative = ref.split(domain, 1)[1].split("#", 1)[0].split("?", 1)[0]
        return (site / relative.lstrip("/")).resolve()
    relative = ref.split("#", 1)[0].split("?", 1)[0]
    return (page.parent / relative).resolve()


#: Sections exempt from the pre-reveal price scan, each for a documented
#: structural reason and nothing else. Buyer Profile: Camarillo quotes the
#: price inside a buyer-objection answer BY DESIGN, so it is allowed there and
#: nowhere else before the reveal. Our Active Multifamily Listings: the table
#: carries OTHER properties' asking prices pulled live from laaa.com, and one
#: of them matching the subject's formatted price to the dollar must not fail
#: the build; the subject's own row is excluded from the feed by sections.py,
#: so the subject's valuation never renders there. Team Track Record: the
#: representative local closings are OTHER buildings' recorded SALE prices from
#: the public track-record feed, and one of LAAA's own past closings happening
#: to have closed at the subject's asking price to the dollar must not fail a
#: build. Nothing in that section renders the subject's valuation: every figure
#: in it is a closed transaction from laaa.com, never a number about this deal.
PRICE_SCAN_EXEMPT = ("track-record", "property-info", "active-listings")


def price_scan_markup(html):
    """Return pre-reveal markup except the calibrated section carve-outs."""
    head = html.split('id="sale-comps"')[0]

    spans = []
    for exempt in PRICE_SCAN_EXEMPT:
        start = head.find(f'id="{exempt}"')
        if start < 0:
            continue
        # Resume at the next locked section that renders after this one, so
        # every other pre-sale-comps section stays inside the scan.
        later = [
            position
            for section in SECTION_ORDER
            if section != exempt
            for position in [head.find(f'id="{section}"', start + 1)]
            if position >= 0
        ]
        spans.append((start, min(later) if later else len(head)))

    kept, cursor = [], 0
    for start, end in sorted(spans):
        kept.append(head[cursor:max(cursor, start)])
        cursor = max(cursor, end)
    kept.append(head[cursor:])
    return "".join(kept)


#: Labelled containers that must never render without content. Each is a
#: heading plus its items; with no items the page publishes a titled empty box.
#: The live 11747 Moorpark page shipped an empty "Key Achievements" panel and an
#: empty "As Featured In" strip because the renderer emitted both
#: unconditionally and NO gate looked at section CONTENT, only at section
#: presence and order. A number-focused human read misses this every time,
#: because nothing is wrong with any number.
EMPTY_CONTAINER_PATTERNS = [
    (r'<div class="condition-note"><div class="condition-note-label">[^<]*</div>\s*<p class="achievements-list">\s*</p>\s*</div>', "Key Achievements panel"),
    (r'<div class="press-strip"><span class="press-strip-label">[^<]*</span>\s*</div>', "As Featured In strip"),
]


#: Every Client Basis sentence renders inside exactly one of these, and
#: nowhere else on the page.
OS_NOTE_PATTERN = re.compile(r'<p class="os-note">.*?</p>', re.S)


def operating_statement_notes_markup(html):
    """Concatenate every Client Basis note paragraph, and nothing else.

    An earlier version of this scan sliced the whole #financials section.
    Codex caught that #financials also carries valuation_narrative and
    disclosure copy, where "market benchmark" or "TOC Tier 3" can be an
    ordinary, correct sentence, not the internal-vocabulary leak this check
    exists to catch (2026-08-09). Client Basis text renders ONLY inside
    <p class="os-note">, so scanning just those paragraphs is narrower than
    #financials and still catches the real defect: the live 11747 Moorpark
    page's "Tier 1 benchmark of $0.11 per square foot" was itself an os-note.
    """
    return "".join(OS_NOTE_PATTERN.findall(html))


#: Matches a note marker at its FIRST kind of appearance: an income line's
#: <sup>[N]</sup> or an expense line's / notes-list <span class="note-ref">[N]</span>.
NOTE_MARKER_INLINE = re.compile(r'(?:<sup>|<span class="note-ref">)\[(\d+)\]')
#: Matches only the Notes to the Operating Statement list's own entries.
NOTE_LIST_ITEM = re.compile(r'<p class="os-note"><span class="note-ref">\[(\d+)\]</span>')


def check_note_marker_reading_order(rel, html):
    """Every note marker must read 1, 2, 3... in the order a reader meets it.

    payload._renumber_notes_in_reading_order assigns marker numbers in
    document reading order as the final step of payload construction, so
    this is a page-level tripwire against future drift, not a duplicate of
    that logic: the live 11747 Moorpark page numbered notes 15, 13, 14, then
    1..12, because markers were allocated in table-build order (expenses
    first, income appended after) rather than reading order (Glen,
    2026-08-09). If sections.py's document order ever changes without the
    reading-order constant changing with it, first-seen order stops matching
    1..N and this fails loudly instead of silently shipping a page where a
    reader meets [15] before [1].
    """
    seen_order, seen_set = [], set()
    for m in NOTE_MARKER_INLINE.finditer(html):
        n = m.group(1)
        if n not in seen_set:
            seen_set.add(n)
            seen_order.append(n)
    if not seen_order:
        return
    expected = [str(i) for i in range(1, len(seen_order) + 1)]
    if seen_order != expected:
        fail(f"{rel}: note markers are not in first-seen reading order. "
             f"got {seen_order}, expected {expected}. A reader must meet "
             f"[1] before [2], never [15] before [1].")

    list_order = [m.group(1) for m in NOTE_LIST_ITEM.finditer(html)]
    if list_order and list_order != sorted(list_order, key=int):
        fail(f"{rel}: the Notes to the Operating Statement list itself is "
             f"not in ascending order: {list_order}.")


def check_no_internal_vocabulary_in_financials(rel, html):
    """Fail a page whose Client Basis text uses internal build-up vocabulary.

    Scoped to the rendered os-note paragraphs, not the whole #financials
    section or the whole page: "benchmark" also appears in ordinary English
    in Sale Comps rationale text (e.g. "closest closed scale and location
    benchmark") and in #financials' own valuation narrative and disclosure
    copy, neither of which is the leak this exists to catch (Codex,
    2026-08-09). The live 11747 Moorpark page printed "Tier 1 benchmark of
    $0.11 per square foot" because a note was transcribed verbatim from the
    internal build-up table instead of translated (Glen, 2026-08-09).
    """
    notes_html = operating_statement_notes_markup(html)
    for pat, label in ((r'>[^<]*\bbenchmark[^<]*<', '"benchmark"'),
                        (r'>[^<]*\btier\s*\d[^<]*<', '"Tier N"')):
        m = re.search(pat, notes_html, re.I)
        if m:
            fail(f"{rel}: an operating-statement note publishes internal "
                 f"methodology vocabulary ({label}): "
                 f"{m.group(0)[1:80].strip()}. Client Basis text must be "
                 f"translated into language a seller reads, not "
                 f"transcribed from the internal build-up table.")


def check_no_empty_labelled_containers(rel, html):
    """Fail a page that renders a labelled container with nothing inside it."""
    for pattern, what in EMPTY_CONTAINER_PATTERNS:
        if re.search(pattern, html):
            fail(f"{rel}: the {what} renders with a heading and no content. "
                 f"A labelled empty box is a defect, not a layout choice: omit "
                 f"the block when the spine supplies nothing for it.")


def check_certified_map_renders(site, referenced_images):
    """Re-hash every map image that the deployable pages reference."""
    map_refs = sorted(
        ref for ref in referenced_images
        if ref.startswith("images/maps-") and ref.lower().endswith(".png")
    )
    if not map_refs:
        return

    manifest = site / "map-manifest.json"
    if not manifest.is_file():
        fail("map-manifest.json is missing, so deployed map bytes cannot be re-verified.")
        return
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"map-manifest.json is not valid JSON: {exc}")
        return
    renders = payload.get("renders")
    if not isinstance(renders, dict):
        fail("map-manifest.json: 'renders' must contain certified SHA-256 digests.")
        return

    for ref in map_refs:
        expected = renders.get(ref)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            fail(f"map-manifest.json has no certified SHA-256 digest for deployed map {ref}.")
            continue
        path = site / ref
        if not path.is_file():
            continue  # the ordinary broken-image check already reports this
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            fail(f"deployed map digest does not match map-manifest.json: {ref}. "
                 f"Regenerate it with the certified maps.py workflow.")


def main(site):
    site = Path(site).resolve()
    if not site.is_dir():
        raise SystemExit(f"not a directory: {site}")

    pages = routes(site)
    if not pages:
        fail("no index.html anywhere. Nothing to deploy.")
        return report(site, pages)

    # Subject prices come from the data file, so the price-discipline check does
    # not depend on how the reveal happens to be marked up.
    prices_by_slug, site_data, property_slugs = {}, {}, set()
    # Unit count per slug, so the mandatory-section rule can tell a land deal
    # from an apartment building that dropped a section it owed the reader.
    payload_units_by_slug: dict[str, int] = {}
    if (site / "bov-site.json").exists():
        try:
            site_data = json.loads((site / "bov-site.json").read_text(encoding="utf-8"))
            for p in site_data.get("properties") or []:
                if p.get("slug"):
                    property_slugs.add(p["slug"])
                    payload_units_by_slug[p["slug"]] = p.get("units") or 0
                if p.get("price"):
                    prices_by_slug[p.get("slug", "")] = f"${p['price']:,.0f}"
            if len(site_data.get("properties") or []) == 1:
                prices_by_slug["__single__"] = next(iter(prices_by_slug.values()), None)
        except (json.JSONDecodeError, KeyError):
            pass   # reported below by the stats check

    referenced_images = set()

    # 1. framework config files
    for name in FRAMEWORK_CONFIGS:
        if (site / name).exists():
            fail(f"{name} present. A BOV website is static HTML, never a framework app.")

    # 2. SPA output in the emitted HTML
    for page in pages:
        html = page.read_text(encoding="utf-8", errors="replace")
        rel = page.relative_to(site)
        for pat, what in SPA_MARKERS:
            if re.search(pat, html, re.I):
                fail(f"{rel}: {what}. The deployed output must be plain static HTML.")

        # 3. both locked webfonts
        if "family=Inter" not in html:
            fail(f"{rel}: Inter is not loaded.")
        if "family=DM+Serif+Display" not in html:
            fail(f"{rel}: DM Serif Display is not loaded. A build with no serif is not Camarillo.")

        # 4. the locked container width
        if "max-width: 1100px" not in html:
            fail(f"{rel}: the 1100px .section container is missing.")

        # 4a. no labelled container may render empty
        check_no_empty_labelled_containers(rel, html)

        # 5. no inlined images
        if "data:image" in html:
            fail(f"{rel}: inlines a base64 image. External files in images/ only.")

        # 6/7. banned map stacks and geocoders
        for pat, what in BANNED_SOURCES:
            if re.search(pat, html, re.I):
                fail(f"{rel}: references {what}. Maps are pre-rendered Google Static Maps "
                     f"from laaa-geo ROOFTOP coordinates.")

        # 8. no API key in client HTML
        if re.search(r'AIza[0-9A-Za-z_\-]{35}', html):
            fail(f"{rel}: a Google API key is present in the emitted HTML.")

        # 9. M&M orange is deck-only
        if re.search(r'#E0843C', html, re.I):
            fail(f"{rel}: M&M orange #E0843C is a deck colour and must not appear on a website.")

        # 10. double-escaped entities
        bad = set(re.findall(r'&amp;(?:[a-zA-Z][a-zA-Z0-9]{1,10}|#[0-9]+);', html))
        if bad:
            fail(f"{rel}: double-escaped entities rendering as literal text: {', '.join(sorted(bad))}")

        # 11. internal segment name in visible copy
        if re.search(r'>[^<]*\bClients\b[^<]*<', html):
            fail(f"{rel}: the internal segment name \"Clients\" appears in visible copy. "
                 f"Client-facing wording is Principals or Property Owners.")

        # 11b. no published confidence level. Rescued from MASTER_PLAN.md, which
        # lived on one laptop and in no repo: "DO NOT display confidence levels
        # on client-facing BOVs... do not use moderate/high confidence in
        # PRICING_RATIONALE text." Confidence is an internal judgement.
        # The word boundaries are load-bearing. They were once literal U+0008
        # backspace characters because a heredoc ate the escape, so the check
        # matched nothing and passed the exact language it exists to catch.
        m = re.search(r'>[^<]*\b(?:high|moderate|low)\s+confidence\b[^<]*<', html, re.I)
        if m:
            fail(f"{rel}: publishes a confidence level ({m.group(0)[1:60].strip()}). "
                 f"Confidence assessment stays in the internal workspace, never on a client page.")

        # 11c. no internal methodology vocabulary in Client Basis text.
        check_no_internal_vocabulary_in_financials(rel, html)

        # 12. no PDF download button on a website
        if "pdf-float-btn" in html or re.search(r'>\s*Download PDF\s*<', html):
            fail(f"{rel}: carries a Download PDF button. Removed from websites (Glen, 2026-07-30).")

        # 13. size sanity. An empty render and a bloated one both signal a broken build.
        kb = len(html.encode()) // 1024
        if kb < 25:
            fail(f"{rel}: only {kb} KB. A real page does not render that small.")
        elif kb > 400:
            warn(f"{rel}: {kb} KB is unusually large for a static page.")

        # 14. every referenced image resolves, including absolute URLs on our
        # own domain. og:image is written absolute, so skipping absolute URLs
        # skipped the share card, which is the one image whose absence is
        # invisible on the page and obvious in every link preview.
        domain = (site / "CNAME").read_text(encoding="utf-8").strip() if (site / "CNAME").exists() else ""
        for ref in image_references(html):
            local = local_image_path(site, page, ref, domain)
            if local is None:
                continue
            if not local.exists():
                fail(f"{rel}: broken image reference {ref}")
                continue
            try:
                referenced_images.add(local.relative_to(site).as_posix())
            except ValueError:
                pass

        # 15. section order
        found = [s for s in re.findall(r'id="([a-z-]+)"', html) if s in SECTION_ORDER]
        expected = [s for s in SECTION_ORDER if s in found]
        if found != expected:
            fail(f"{rel}: sections are out of the locked order.\n"
                 f"        got      {found}\n        expected {expected}")
        # Ordering only the IDs that happen to exist let a page delete an
        # entire required section and still pass. Property pages have a fixed
        # mandatory spine; Photos and On-Market remain data-conditional.
        is_property_page = (
            (len(property_slugs) == 1 and page == site / "index.html")
            or page.parent.name in property_slugs
        )
        if is_property_page:
            mandatory = {
                "track-record", "marketing", "investment", "location",
                "prop-details", "property-info", "rent-comps", "sale-comps",
                "opinion-of-value", "financials", "contact",
            }
            # Rent Comps is mandatory because an apartment BOV that quietly
            # dropped it would be hiding its rent evidence. A land deal has no
            # units to compare and the generator omits the section by design, so
            # requiring it here would demand a section with nothing in it. Read
            # from the payload rather than guessed from the HTML.
            if not (payload_units_by_slug.get(page.parent.name)
                    if page.parent.name in property_slugs
                    else next(iter(payload_units_by_slug.values()), None)):
                mandatory.discard("rent-comps")
            missing = [s for s in SECTION_ORDER if s in mandatory and s not in found]
            if missing:
                fail(f"{rel}: missing mandatory section(s): {', '.join(missing)}")

        # 15b. note markers read 1, 2, 3... in first-seen document order.
        check_note_marker_reading_order(rel, html)

        # 16. BOV price discipline, with the documented Camarillo carve-out.
        #
        # The price comes from the DATA, not from scraping the page. Scraping
        # for "RECOMMENDED LIST PRICE" missed it entirely, because the caps are
        # CSS text-transform and the markup says "Recommended List Price". A
        # gate that silently matches nothing reports PASS forever.
        if "sale-comps" in found and "financials" in found and prices_by_slug:
            token = prices_by_slug.get(page.parent.name) or prices_by_slug.get("__single__")
            if token:
                # Camarillo quotes the price inside a buyer-objection answer in
                # #property-info. That IS the locked design, so it is allowed
                # there and nowhere else before the reveal. Rent Comps follows
                # Buyer Profile and must still be scanned.
                if token in price_scan_markup(html):
                    fail(f"{rel}: the subject price {token} appears before #sale-comps. "
                         f"On a BOV the valuation reveal belongs in Financial Analysis.")

    # Re-run the certified-render hashes on the exact map files referenced by
    # deployable pages. The build-time schema check is not enough: a PNG can be
    # changed after generation but before workflow_dispatch.
    check_certified_map_renders(site, referenced_images)

    # 17. required files
    for name in REQUIRED_FILES:
        if not (site / name).exists():
            fail(f"missing required file: {name}")

    # 18. the published figures must come from a live pull FOR THIS BUILD.
    #
    # `stats.live` alone is not evidence. An old bov-site.json still carries
    # live=true, and a missing one used to be a warning, so either an omitted
    # file or a reused one let a deploy through without proving anything. Three
    # things are required now: the file exists, the flag is true, and the pages
    # themselves carry the timestamp of the pull they were rendered from.
    data = site / "bov-site.json"
    if not data.exists():
        fail("no bov-site.json. The published track-record and marketing figures cannot be "
             "shown to have come from a live pull, so this build is not provably current.")
    else:
        try:
            payload = json.loads(data.read_text(encoding="utf-8"))
            stats = payload.get("stats") or {}
            if payload.get("document_type") != "bov":
                fail("bov-site.json must explicitly declare document_type='bov'.")
        except json.JSONDecodeError as exc:
            fail(f"bov-site.json is not valid JSON: {exc}")
            stats = {}
        if stats.get("live") is not True:
            fail("stats.live is not true. This build used fallback figures rather than a live "
                 "pull, so its track-record and marketing numbers may be stale. Rebuild online.")
        generated = stats.get("generated")
        if not generated:
            fail("stats.generated is missing, so the figures cannot be dated.")
        else:
            try:
                generated_at = datetime.fromisoformat(generated)
                if generated_at.tzinfo is None or generated_at.utcoffset() is None:
                    fail("stats.generated must include an explicit timezone offset.")
                else:
                    age = (datetime.now(timezone.utc) - generated_at).total_seconds() / 3600
                    if age > MAX_FIGURE_AGE_HOURS:
                        exempt = validated_sha_exemption(site)
                        if exempt:
                            warn(f"the live figures are {age:.0f} hours old "
                                 f"(limit {MAX_FIGURE_AGE_HOURS}), published under the "
                                 f"validated-SHA exemption for {exempt[:12]}: this exact "
                                 f"commit already passed the gate, and re-publishing "
                                 f"approved bytes must not force a rebuild that changes them.")
                        else:
                            fail(f"the live figures are {age:.0f} hours old "
                                 f"(limit {MAX_FIGURE_AGE_HOURS}). "
                                 f"Rebuild so the published numbers are current.")
            except (TypeError, ValueError):
                fail(f"stats.generated is not a valid timestamp: {generated!r}")
            # Every rendered page must carry the same pull it was built from.
            for page in pages:
                stamp = re.search(r'name="laaa-figures-pulled" content="([^"]*)"',
                                  page.read_text(encoding="utf-8", errors="replace"))
                got = stamp.group(1) if stamp else ""
                if got != generated:
                    fail(f"{page.relative_to(site)} was rendered from a different pull than "
                         f"bov-site.json records ({got or 'no stamp'} vs {generated}). "
                         f"The data file and the pages are out of sync; rebuild.")

    # 19. DEPLOY-ARTIFACT SCOPE. A deal repository is public and
    # raw.githubusercontent.com serves every tracked file in it, so the question
    # is not "what does Pages publish" but "what would this tree commit".
    #
    # An ALLOWLIST, never a blocklist: a file nobody anticipated is refused
    # rather than published. A blocklist would have had to name
    # approval-record.json in advance, and nobody did. This gate classifies
    # files, never their contents; addresses and escrow references are ordinary
    # BOV substance and nothing here inspects a string.
    #
    # BLOCKING is declared here rather than left to the text match below. Every
    # message this emits names a FILE and a class of artifact, deliberately
    # never a price, an address or a credential, so it matched none of the
    # blocking patterns and the entire check printed as advisory.
    for message in _site_artifacts().artifact_scope_errors(site, property_slugs):
        fail(message, blocking=True)

    return report(site, pages)


# June Mode, 2026-08-13: this gate is SPLIT BY FINDING CLASS rather than
# demoted wholesale. A finding blocks only when it is a wrong fact or a
# structural violation with a real consequence. Aesthetics and prose warn.
#
# Blocking classes, matched against the finding text:
#   architecture  the no-framework ban (React/Vite/Next/SPA/bundler on a
#                 website). The Blix/Brio scar; not waivable by anyone.
#   property      wrong property, address, APN, or slug binding.
#   financial     price, cap, GRM, $/unit, $/SF correctness.
#   secrets       an API key or credential reaching emitted HTML.
#   required      a required file missing from the shipped artifact.
#   maps          a certified render whose deployed bytes fail digest
#                 re-verification, or a manifest that cannot certify them.
#                 A tampered or uncertifiable pin is a wrong public fact
#                 (the 3837 College 33m street pin), and the whole point of
#                 check_certified_map_renders is catching bytes changed AFTER
#                 the build gates ran. Every refusal it emits names
#                 map-manifest.json, which is what the pattern matches. The
#                 original June Mode split missed this class, so the gate
#                 printed the tamper finding as advisory and exited 0.
# Everything else (fonts, colors, section order, nav, spacing, copy, image
# dimensions) reports and continues.
#
# One class is NOT matched here and must not be:
#   confidentiality  an internal deliberation artifact in a public deploy
#                 repository (check 19). It is declared at the call site with
#                 `fail(..., blocking=True)`, because its messages name a file
#                 and an artifact class and never quote content, so there is no
#                 honest substring to match. The same split that missed maps
#                 missed this one, and the consequence is the exact leak
#                 site_artifacts.py exists to stop.
_BLOCKING_PATTERNS = (
    "framework", "react", "vite", "next.js", "svelte", "astro", "remix",
    "slidev", "bundler", "hydration", "module script", "/assets/index-",
    "apn", "address", "slug", "wrong property", "property mismatch",
    "price", "cap rate", "grm", "$/unit", "$/sf", "per unit", "per sf",
    "api key", "credential", "secret", "token",
    "missing", "not found", "required file", "absent",
    "map-manifest",
)


def _is_blocking(message):
    m = str(message).lower()
    return any(p in m for p in _BLOCKING_PATTERNS)


def _blocks(message, declared):
    """A class declared by the raising check wins; text matching is the fallback."""
    return _is_blocking(message) if declared is None else declared


def report(site, pages):
    print(f"\nBOV SITE GATE  {site}")
    print(f"checked {len(pages)} route file(s): {', '.join(str(p.relative_to(site)) for p in pages)}\n")
    blocking = [m for m, declared in fails if _blocks(m, declared)]
    advisory = [m for m, declared in fails if not _blocks(m, declared)]
    for w in warns:
        print(f"  warn  {w}")
    for a in advisory:
        print(f"  advisory  {a}")
    for f in blocking:
        print(f"  FAIL  {f}")
    if not blocking:
        print(
            f"\n  PASS  {len(warns) + len(advisory)} advisory finding(s), nothing blocking. "
            "Safe to commit and deploy. Advisory findings create no work unless Glen asks."
        )
        return 0
    print(f"\n  {len(blocking)} BLOCKING failure(s) (wrong fact or architecture). Do not commit or deploy.")
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__.strip().splitlines()[2].strip())
    sys.exit(main(sys.argv[1]))
