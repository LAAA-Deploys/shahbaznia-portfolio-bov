"""Generate the deal-repo ``bov-site.json`` presentation payload from the spine."""

from __future__ import annotations

from contextlib import contextmanager
import copy
from copy import deepcopy
import hashlib
import json
from math import isclose
from pathlib import Path
import shutil
from typing import Any

from reporting.bovsite.contracts.canonical import canonical_sha256
from reporting.bovsite.contracts.domains import (
    domain_hashes,
    portfolio_domain_hashes,
    project_domains,
)
from reporting.bovsite.contracts.loader import write_json_atomic
from reporting.bovsite.contracts.workspace import DealWorkspace
from reporting.bovsite.copy_compile import publication_errors


class PayloadError(ValueError):
    pass


class _CopyProblems:
    """Collect every copy-block problem in one build instead of the first only.

    Each missing or malformed ``blocks.<section>.<field>`` used to raise on
    sight, so an author fixed one block, reran the whole build, and met the
    next one: a serial loop whose length is the number of missing blocks.
    ``copy.schema.json`` cannot catch them because ``blocks`` is a free-form
    object, so this is the only place they are knowable. While a collector is
    active the readers record the problem and return an empty value; nothing
    between the reads and the raise does arithmetic on copy text, so the
    placeholders are inert and every problem is reported together.
    """

    def __init__(self) -> None:
        self.problems: list[str] = []

    def record(self, message: str) -> None:
        if message not in self.problems:
            self.problems.append(message)


#: Active collector, or None when the readers should raise immediately.
_copy_problems: _CopyProblems | None = None


def _copy_problem(message: str) -> None:
    """Record a copy problem, or raise now when nothing is collecting."""
    if _copy_problems is None:
        raise PayloadError(message)
    _copy_problems.record(message)


def _copy_problem_report(problems: list[str]) -> str:
    return f"copy.json has {len(problems)} problem(s):\n  - " + "\n  - ".join(problems)


def _raise_collected_copy_problems() -> None:
    """Report collected problems NOW, before an invariant reads a placeholder.

    A collected problem leaves an empty placeholder behind. Any invariant that
    then compares those placeholders raises its own, less actionable error,
    which escapes the collector's block and hides the report this exists to
    produce. Call this the moment collection for a stage is complete.
    """
    if _copy_problems is not None and _copy_problems.problems:
        raise PayloadError(_copy_problem_report(_copy_problems.problems))


@contextmanager
def _collect_copy_problems():
    """Batch copy problems across everything built inside this block.

    A portfolio builds each member through ``build_payload``, so the OUTERMOST
    block owns the report: nesting would otherwise stop at the first member
    that has a problem, which is the same serial loop one level up.
    """
    global _copy_problems
    if _copy_problems is not None:
        yield _copy_problems
        return
    collector = _CopyProblems()
    _copy_problems = collector
    try:
        yield collector
    finally:
        _copy_problems = None
    if collector.problems:
        raise PayloadError(_copy_problem_report(collector.problems))


def _paragraphs(copy: dict, section: str, field: str, *, required: bool = True) -> list[str]:
    values = (copy.get("blocks", {}).get(section, {}) or {}).get(field)
    if values is None:
        if required:
            _copy_problem(f"copy.json is missing blocks.{section}.{field}")
        return []
    return list(values)


def _achievement_pairs(values: list[str]) -> list[list[str]]:
    """Split each authored achievement into the (award, qualifier) pair the page renders.

    blocks.track_record emits ``<strong>{a[0]}</strong> - {a[1]}``, so a plain
    string indexes to its first two CHARACTERS: "Chairman's Club | ..." rendered
    as "C - h". Camarillo escaped it only because its payload predates the copy
    spine and carried real pairs. Authors separate the award from its qualifier
    with a pipe; an achievement written without one renders as the award alone.
    """
    pairs: list[list[str]] = []
    for value in values:
        award, separator, qualifier = str(value).partition("|")
        pairs.append([award.strip(), qualifier.strip()] if separator else [award.strip(), ""])
    return pairs


def _one(copy: dict, section: str, field: str) -> str:
    # PRESENT-but-wrong and ABSENT are different problems. copy.schema.json
    # permits an empty array, so `[]` is a real authored value that must still
    # fail the exact-one rule; testing the returned list alone would treat it
    # as "already reported missing" and ship a blank client-facing string.
    present = (copy.get("blocks", {}).get(section, {}) or {}).get(field) is not None
    values = _paragraphs(copy, section, field)
    if len(values) != 1:
        # A missing block already recorded its own problem; do not report the
        # same block twice as "missing" and "not exactly one".
        if present:
            _copy_problem(
                f"blocks.{section}.{field} must contain exactly one string, "
                f"not {len(values)}")
        return ""
    return values[0]


def _paired(copy: dict, section: str, titles: str, bodies: str,
            *, required: bool = True) -> list[dict[str, str]]:
    title_values = _paragraphs(copy, section, titles, required=required)
    body_values = _paragraphs(copy, section, bodies, required=required)
    if len(title_values) != len(body_values):
        _copy_problem(
            f"blocks.{section}.{titles} has {len(title_values)} entries but "
            f".{bodies} has {len(body_values)}; they must have equal lengths")
        return []
    return [
        {"title": title, "copy": body}
        for title, body in zip(title_values, body_values, strict=True)
    ]


#: The one place a declared fraction becomes a rendered percent.
#:
#: This used to be `value * 100 if abs(value) <= 1 else value`, which GUESSED
#: the unit from magnitude. A comp cap of exactly 1.0 (meaning 1%) rendered as
#: 100%, and a genuine 0.9% rendered as 90%. Guessing is the same defect that
#: printed "Interest Rate 0.06%": the contract now declares the form
#: (comps-sale.cap_rate is a bounded fraction) and this converts it, once.
def _fraction_to_percent(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 100


def _money_range(low: float, high: float) -> str:
    return f"${low:,.0f} to ${high:,.0f}"


#: Stated dollar tolerance on every tax-anchor equality. A cent-level rounding
#: difference between the keyed model and ``price * rate`` is not a mis-anchored
#: NOI; anything larger is.
_ANCHOR_TOLERANCE = 1.0


def tax_anchor_problems(
    price: float, tax_rate: Any, anchor: Any, expense_rows: list[list[Any]],
    modeled_totals: tuple[float, float] | None = None,
) -> list[str]:
    """Verify the NOI's tax basis before any cap may publish. Returns problems.

    Every published cap on the page divides an NOI by a price, and the ladder
    derives each rung by moving that NOI by ``(rung - recommended) * tax_rate``.
    Both are reassessments only if the approved NOI was itself struck with taxes
    reassessed at the recommended price. ``pricing.operations.*.noi`` is the
    keyed model's figure and its tax line can carry any basis, most often the
    seller's Prop 13 T12 bill; when it does, every published cap is wrong by a
    constant, including the recommended-price row where the delta is zero. The
    engine cannot infer the basis, because a tax bill is an ad valorem component
    plus flat direct assessments that do not scale with price, so the contract
    declares both components and this reconciles them.

    Three equalities, each within ``_ANCHOR_TOLERANCE``:

    1. ``anchor.ad_valorem == price * tax_rate`` — the declared value-based
       dollars are the reassessed figure.
    2. The expense line named by ``anchor.expense_line`` exists, by exact label.
       Never pattern-matched: "Real Estate Taxes", "Property Taxes" and "Taxes"
       are all real labels in this repository, and guessing between them is the
       name-matching shortcut Hard Rule 13 warns about.
    3. Each of that line's current AND pro forma columns equals
       ``ad_valorem + flat_assessments``. An EQUALITY, not a one-sided bound:
       ``line >= ad_valorem`` proves the line is large enough but not that its
       remainder is the flat assessment. A $35,000 line holding the old $30,000
       ad valorem plus a real $5,000 flat assessment passes one-sided while the
       reassessed line should be $36,250, so the NOI stays $1,250 high and the
       cap is still unreassessed.
    4. With ``modeled_totals`` (current, pro forma operating expenses from
       ``pricing.operations``), each column of the rows SUMS to the modeled
       total, and the named label matches exactly one row. The row equality in
       (3) proves nothing about the NOI unless the rows ARE the NOI's expenses:
       supplied ``presentation.expense_lines`` are otherwise display-only, so a
       $31,250 display row with a matching anchor would pass while the modeled
       NOI still carried the old $30,000 tax. Generated rows reconcile to the
       modeled totals by construction; supplied rows must prove it.

    The caller refuses the whole build on any problem rather than withholding a
    cap column: the figures are not missing, they are false, and a BOV that
    silently drops its cap rate is a different defect from one that should never
    have been generated. The analyst's approved, three-way model-reconciled
    operating statement is never restated to fit the page; an unanchored spine
    is refused, not rewritten.
    """
    if anchor is None:
        return [
            "the deal publishes a cap (pricing.decision.cap_rate is set) but "
            "financials.presentation.tax_anchor is not declared, so nothing "
            "proves the approved NOI's taxes were reassessed at the recommended "
            "price. Declare ad_valorem, flat_assessments and expense_line, or "
            "withhold the cap by setting decision.cap_rate to null."
        ]
    problems = []
    ad_valorem = anchor["ad_valorem"]
    flat = anchor["flat_assessments"]
    label = anchor["expense_line"]
    reassessed = price * (tax_rate / 100.0)
    if abs(ad_valorem - reassessed) > _ANCHOR_TOLERANCE:
        problems.append(
            f"tax_anchor.ad_valorem is {ad_valorem:,.2f} but taxes reassessed at "
            f"the recommended price are {price:,.0f} * {tax_rate}% = "
            f"{reassessed:,.2f}. The declared NOI tax basis is not the "
            f"reassessed figure, so no published cap would be a reassessment."
        )
    if modeled_totals is not None:
        for column, index, total in (("current", 1, modeled_totals[0]),
                                     ("pro forma", 2, modeled_totals[1])):
            shown = sum(r[index] for r in expense_rows)
            if abs(shown - total) > _ANCHOR_TOLERANCE:
                problems.append(
                    f"the {column} expense lines sum to {shown:,.2f} but the "
                    f"modeled operating expenses are {total:,.2f}. The displayed "
                    f"lines are not the NOI's expenses, so no line equality can "
                    f"prove the NOI's tax basis."
                )
    matches = [r for r in expense_rows if r[0] == label]
    if len(matches) > 1:
        problems.append(
            f"tax_anchor.expense_line {label!r} matches {len(matches)} expense "
            f"lines. The anchor must bind to exactly one row."
        )
        return problems
    row = matches[0] if matches else None
    if row is None:
        found = ", ".join(repr(r[0]) for r in expense_rows) or "none"
        problems.append(
            f"tax_anchor.expense_line {label!r} matches no expense line "
            f"(found: {found}). Name the exact line carrying the property tax; "
            f"the engine never pattern-matches a label."
        )
        return problems
    expected = ad_valorem + flat
    for column, value in (("current", row[1]), ("pro forma", row[2])):
        if abs(value - expected) > _ANCHOR_TOLERANCE:
            problems.append(
                f"expense line {label!r} {column} column is {value:,.2f} but the "
                f"declared anchor reconciles to ad_valorem {ad_valorem:,.2f} + "
                f"flat_assessments {flat:,.2f} = {expected:,.2f}. The line's "
                f"remainder is not the declared flat assessment, so the NOI's "
                f"tax basis is unproven and every published cap would be "
                f"unreassessed."
            )
    return problems


def _opinion_of_value(
    decision: dict, current: dict, market: dict, tax_rate: Any,
    units: int, building_sf: float, lot_sf: float | None = None,
    buildable_units: int | None = None,
) -> list[dict]:
    """The four-rung opinion of value, priced with taxes reassessed per rung.

    Property taxes reassess at the sale price, so NOI is different at every rung
    and reusing one NOI would misstate the cap on three rows out of four. Any
    flat per-parcel component of the bill cancels in the difference between two
    rungs, so the whole adjustment is ``(rung - recommended) * tax_rate`` and the
    declared ``tax_rate`` alone is enough to derive it.

    ``tax_rate`` is percent-as-percent on the presentation block, matching the
    caps below, so it is divided by 100 exactly once here.

    The ask legitimately sits ABOVE the range top: that is what an ask is.
    ``decision.cap_rate`` remains the publish control — when the analyst withheld
    the cap, every rung withholds it too rather than leaking it into this table.

    On a land deal ``units`` is 0 and scheduled rent may be 0, so $/unit and GRM
    are UNDEFINED, not merely unknown. They come back None and the renderer drops
    the columns, the same way it already drops the caps. Dividing by zero to fill
    a cell, or printing a dash under a live column head, would both invite a
    reader to compare a land price against apartment metrics that do not exist.
    ``lot_sf`` and ``buildable_units`` are the metrics that DO mean something on
    such a deal, and they are None on an ordinary apartment building.
    """
    price = decision["exact_price"]
    low, high = decision["range_low"], decision["range_high"]
    rate = (tax_rate or 0) / 100.0
    # A zero or absent reassessment rate is a legitimate contract state: taxes
    # may be declared only as an expense line. But without a rate there is no
    # way to reassess per rung, and publishing caps anyway would print four
    # figures under a sentence claiming they were reassessed. The claim and the
    # figures stand or fall together, so both are withheld. schema.py reports it
    # rather than letting the table quietly lose a column.
    publish_cap = decision.get("cap_rate") is not None and rate > 0

    gsr_current = current["gross_scheduled_rent"]
    gsr_market = market["gross_scheduled_rent"]

    def rung(label: str, rung_price: float) -> dict:
        delta = (rung_price - price) * rate
        noi_current = current["noi"] - delta
        noi_market = market["noi"] - delta
        return {
            "label": label,
            "price": rung_price,
            "price_per_unit": rung_price / units if units else None,
            "price_per_sf": rung_price / building_sf if building_sf else None,
            "price_per_land_sf": rung_price / lot_sf if lot_sf else None,
            "price_per_buildable_unit": (
                rung_price / buildable_units if buildable_units else None
            ),
            "grm_current": rung_price / gsr_current if gsr_current else None,
            "grm_market": rung_price / gsr_market if gsr_market else None,
            "cap_current": noi_current / rung_price * 100 if publish_cap else None,
            "cap_market": noi_market / rung_price * 100 if publish_cap else None,
        }

    return [
        rung("Suggested list price", price),
        rung("Range top", high),
        rung("Midpoint", (low + high) / 2),
        rung("Range bottom", low),
    ]


#: Income lines of the operating statement that represent an underwriting
#: DECISION rather than arithmetic. Derived rows (gross operating income, the
#: expense total, NOI, and every financing row) are computed from the lines
#: above them and carry no basis of their own.
INCOME_DECISION_LINES = (
    ("scheduled_gross_income", "Scheduled Gross Income"),
    ("vacancy", "Vacancy Reserve"),
    ("credit_loss", "Credit Loss"),
    ("additional_income", "Additional Income"),
)

#: Ledger fields that are INTERNAL deliberation. They never enter a payload:
#: a confidence level must never reach a client-facing BOV, and the analyst's
#: defensibility rationale is not the seller's note.
LEDGER_INTERNAL_FIELDS = ("reason", "confidence", "glen_approval_needed")


def _next_marker(notes: dict) -> str:
    """Lowest positive integer key not already taken.

    Length-based allocation collides when authored keys are sparse: notes
    {"1", "3"} has length 2, so len+1 is "3" and a new marker would overwrite
    an authored expense note with an income one.
    """
    taken = {str(key) for key in notes}
    candidate = 1
    while str(candidate) in taken:
        candidate += 1
    return str(candidate)


def client_basis_by_line(financials: dict) -> dict[str, str]:
    """Map each ledger line to the ONE sentence the seller may read.

    The basis is decided during underwriting, when the analyst chooses the
    seller's number or an estimate, high or low in the band, per unit or per
    square foot. Carrying it as data means the BOV derives the note instead of
    an author retyping it at build time, which is how a note once claimed a
    line contained a fee it did not.
    """
    out: dict[str, str] = {}
    for entry in financials.get("decision_ledger") or []:
        key = str(entry["line"]).strip().casefold()
        if key in out and out[key] != entry["client_basis"]:
            raise PayloadError(
                f"decision_ledger has two entries for {entry['line']!r} with "
                f"different client_basis text. One line states one basis; "
                f"silently keeping the last would publish whichever happened "
                f"to be written second. first says {out[key]!r}; second says "
                f"{entry['client_basis']!r}.")
        out[key] = entry["client_basis"]
    return out


#: The order a reader actually encounters each note-bearing line on the page,
#: coupled to sections.py's own table order: the Unit Mix & Scheduled Rent
#: table's Additional Income row renders BEFORE the Annualized Operating Data
#: table's Scheduled Gross Income, Vacancy and Credit Loss rows, which render
#: BEFORE the Annualized Expenses table (Glen, 2026-08-09). If sections.py's
#: table order ever changes, this constant must change with it, or
#: check_bov_site.py's note-marker-reading-order gate starts failing every
#: build until it does — that gate is what makes the coupling loud instead of
#: silent.
NOTE_READING_ORDER_INCOME_KEYS = (
    "additional_income", "scheduled_gross_income", "vacancy", "credit_loss",
)


def _renumber_notes_in_reading_order(prop: dict) -> None:
    """Remap the whole note registry so marker numbers follow READING order.

    Markers are ALLOCATED in whatever order the payload happens to build rows
    (expense lines first, income appended after), which is not the order a
    reader encounters them. The live 11747 Moorpark page numbered notes 15,
    13, 14, 1..12, because Additional Income renders in the Unit Mix table,
    ABOVE the Annualized Operating Data table carrying Scheduled Gross Income
    and Vacancy, which renders above the Annualized Expenses table (Glen,
    2026-08-09). This is the final step of payload construction: ONE
    numbering, assigned here, shared by the payload and the page, so [1] is
    always the first note a reader meets.
    """
    old_notes = prop.get("expense_notes") or {}
    old_income = prop.get("income_notes") or {}
    if not old_notes:
        return

    reading_order = [
        old_income[key]
        for key in NOTE_READING_ORDER_INCOME_KEYS
        if key in old_income
    ]
    # Only a VISIBLE expense line — sections.py drops a row that is $0 in
    # both columns entirely, per the Brio trash lesson, even though schema.py
    # still requires that row to carry a note. Counting a hidden row here
    # would burn a slot in the gapless 1..N sequence for a marker nobody ever
    # sees, and check_bov_site.py's reading-order gate would then reject an
    # honest page for a gap that is not actually visible on it (Bugbot,
    # 2026-08-09).
    reading_order += [
        str(row[3]) for row in (prop.get("expense_lines") or [])
        if len(row) > 3 and row[3] is not None and (row[1] or row[2])
    ]

    # A hidden ($0/$0) expense line still needs SOME marker: schema.py
    # requires every expense line to carry a note regardless of whether the
    # row renders. Number these AFTER every visible marker so the visible
    # sequence stays gapless; the number itself never appears on the page,
    # since sections.py never prints that row (Bugbot, 2026-08-09).
    referenced = set(reading_order)
    reading_order += [
        str(row[3]) for row in (prop.get("expense_lines") or [])
        if len(row) > 3 and row[3] is not None and str(row[3]) not in referenced
    ]

    # Two rows may legitimately cite the SAME note: one explanation can cover
    # two lines, and the contract permits it. Keep only each marker's FIRST
    # occurrence before numbering. The comprehension below keeps the LAST
    # position for a repeated key, so two visible rows citing "1" would both
    # remap to "2": the page's first marker would read [2], nothing would
    # carry [1], and check_bov_site.py's reading-order gate would correctly
    # reject a page that is otherwise honest (Codex, 2026-08-09).
    reading_order = list(dict.fromkeys(reading_order))

    remap = {str(old): str(new) for new, old in enumerate(reading_order, start=1)}
    if not remap or all(old == new for old, new in remap.items()):
        return

    # A note key present in the registry but referenced by no income key and
    # no expense row is a stray authored entry: nothing on the page, hidden
    # or visible, ever resolves it. It was already dead data before this
    # function ran, so it is dropped here rather than crashing on a key
    # absent from remap (Bugbot, 2026-08-09).
    prop["expense_notes"] = {
        remap[old]: content for old, content in old_notes.items()
        if old in remap
    }
    prop["income_notes"] = {
        key: remap[str(marker)] for key, marker in old_income.items()
    }
    for row in prop.get("expense_lines") or []:
        if len(row) > 3 and row[3] is not None:
            row[3] = remap[str(row[3])]


def _expense_rows(
    financials: dict, current: dict, market: dict
) -> tuple[list[list[Any]], dict[str, list[str]]]:
    """Return expense rows and the complete note mapping those rows reference.

    Markers are allocated here, once, and only for a line that actually carries
    a note, so a line can never print a marker that resolves to another line's
    explanation. Allocation happens during payload generation, before
    ``presentation_projections`` snapshots the payload and before approval
    binding; the renderer only consumes the finished mapping.
    """
    presentation = financials.get("presentation") or {}
    supplied_notes = presentation.get("expense_notes") or {}
    # The ledger is the authored-at-underwriting-time source and outranks the
    # generated basis sentence; an explicitly supplied note still wins over
    # both, because it is the most specific thing anyone wrote.
    ledger = client_basis_by_line(financials)
    supplied = presentation.get("expense_lines")
    if supplied is not None:
        rows = deepcopy(supplied)
        notes = deepcopy(supplied_notes)
        # Hand-listed lines take their basis from the ledger too, so a deal
        # that supplies its own display rows still never retypes a note. Same
        # one-source rule as the generated path: a row already pointing at an
        # authored note that DISAGREES with the ledger refuses rather than
        # letting two sentences compete for one line.
        for row in rows:
            from_ledger = ledger.get(str(row[0]).strip().casefold())
            if not from_ledger:
                continue
            if row[3] is None:
                marker = _next_marker(notes)
                notes[marker] = [row[0], from_ledger]
                row[3] = marker
                continue
            existing = notes.get(str(row[3]))
            if existing and list(existing) != [row[0], from_ledger]:
                raise PayloadError(
                    f"expense line {row[0]!r} has both a decision-ledger basis "
                    f"and a different hand-authored note. The basis has one "
                    f"source: edit the ledger entry, which reopens "
                    f"financial-inputs approval, rather than retyping it here. "
                    f"ledger says {from_ledger!r}; the authored note says "
                    f"{list(existing)[-1]!r}."
                )
        return rows, notes
    grouped: dict[str, float] = {}
    basis_by_category: dict[str, set] = {}
    for row in financials["t12"]["rows"]:
        if row["classification"] != "parsed" or row["category"] in {
            "gross_scheduled_rent",
            "other_income",
            "vacancy",
            "credit_loss",
        }:
            continue
        grouped[row["category"]] = grouped.get(row["category"], 0) + (row["annual"] or 0)
        basis_by_category.setdefault(row["category"], set()).add(row.get("basis"))
    rows: list[list[Any]] = []
    notes: dict[str, list[str]] = {}

    def add(label: str, current_amount: float, market_amount: float, note: Any) -> None:
        if note is None:
            rows.append([label, current_amount, market_amount, None])
            return
        marker = _next_marker(notes)
        notes[marker] = list(note)
        rows.append([label, current_amount, market_amount, marker])

    # Note key N is authored against the Nth generated line. Reading it here is
    # what binds a marker to its own line rather than to whatever number the
    # line happened to sit at.
    #: What a generated line may truthfully say about itself, keyed on the
    #: basis the row DECLARES. A parsed row is not automatically a seller
    #: actual: `annual_estimate` covers LAAA benchmark build-ups and rent-roll
    #: derived figures, and calling one a T12 actual would put a false
    #: provenance statement on a client-facing page.
    basis_note = {
        "t12_actuals": "Taken from the seller's T12.",
        "annual_estimate": "Underwritten annual estimate, not a seller T12 actual.",
    }
    for position, (category, amount) in enumerate(sorted(grouped.items()), start=1):
        label = category.replace("_", " ").title()
        # EVERY line states its basis (Glen, 2026-08-09), and the basis has ONE
        # source. Precedence, in order:
        #
        #   1. The decision ledger, written by the analyst during underwriting
        #      at the moment it chose the seller's number or an estimate, high
        #      or low in the band, per unit or per square foot.
        #   2. Otherwise the row's own declared t12 basis, for a generated row.
        #   3. Otherwise nothing, and schema.py refuses the build naming the
        #      line, so an author states the basis rather than the engine
        #      guessing one.
        #
        # A hand-authored note that DISAGREES with the ledger refuses rather
        # than quietly winning. Two sources for one published sentence is the
        # retyping channel this design exists to close: it is what let a note
        # claim a line contained a fee it did not. Correcting a basis means
        # editing the ledger, which correctly reopens financial-inputs
        # approval.
        #
        # A category aggregates several rows, so a t12-derived basis may only
        # be stated when its rows AGREE. Keeping just the first row's basis
        # would let a mixed category claim a provenance false for part of the
        # printed total.
        declared = basis_by_category.get(category) or set()
        from_ledger = ledger.get(label.strip().casefold())
        authored = supplied_notes.get(str(position))
        if from_ledger and authored and list(authored) != [label, from_ledger]:
            raise PayloadError(
                f"expense line {label!r} has both a decision-ledger basis and a "
                f"different hand-authored note. The basis has one source: edit "
                f"the ledger entry, which reopens financial-inputs approval, "
                f"rather than retyping it here. "
                f"ledger says {from_ledger!r}; the authored note says "
                f"{list(authored)[-1]!r}."
            )
        note = None
        if from_ledger:
            note = [label, from_ledger]
        elif authored:
            note = authored
        elif len(declared) == 1:
            basis_text = basis_note.get(next(iter(declared)))
            note = [label, basis_text] if basis_text else None
        add(label, amount, amount, note)
    classified_total = sum(grouped.values())
    current_adjustment = current["operating_expenses"] - classified_total
    market_adjustment = market["operating_expenses"] - classified_total
    if not (
        isclose(current_adjustment, 0, abs_tol=0.01)
        and isclose(market_adjustment, 0, abs_tol=0.01)
    ):
        add(
            "Underwriting Expense Adjustment",
            current_adjustment,
            market_adjustment,
            [
                "Underwriting Expense Adjustment",
                "Aggregate difference between classified T12 expense lines and the modeled "
                "current and pro forma operating expense totals.",
            ],
        )
    return rows, notes


def presentation_projections(payload: dict) -> dict[str, Any]:
    """Return public-payload subsets used to catch post-build hand edits.

    These are a generated-artifact integrity binding, not a second approval
    rule.  Approval validity remains exclusively the canonical domain hashes.
    """
    properties = payload.get("properties") or []
    identity_fields = (
        "slug", "short_name", "address", "city", "submarket",
        "track_record_submarkets", "apn", "units",
        "building_sf", "lot_sf", "lot_acres", "year_built", "renovated_year",
        "parking", "maps",
    )
    financial_fields = (
        "scheduled_rent", "monthly_sgi", "additional_income", "noi_current",
        "noi_market", "expense_total", "expense_per_unit", "expense_per_sf", "unit_mix",
        "operating", "expense_lines", "expense_notes", "income_notes",
        "tax_rate", "tax_anchor",
        "financing_structure", "financing",
    )
    pricing_fields = (
        "price", "price_per_unit", "price_per_sf", "value_range", "opinion_of_value",
        "grm_current",
        "grm_market", "cap_current", "cap_market", "cap_current_basis", "cap_market_basis",
        "show_buyout_model", "buyout_model",
        "show_value_scenarios", "value_scenarios",
    )
    copy_fields = (
        "highlights", "overview", "location_title", "location_narrative",
        "physical_narrative", "market_narrative", "positioning_narrative",
        "rent_narrative", "valuation_narrative", "strategy", "buyer_profiles", "disclosures",
    )
    return {
        "identity": {
            "meta": payload.get("meta"),
            "team": payload.get("team"),
            "properties": [
                {"deal_id": prop.get("deal_id"), **{key: prop.get(key) for key in identity_fields}}
                for prop in properties
            ],
            "portfolio_total_units": (payload.get("portfolio") or {}).get("total_units"),
        },
        "financial_inputs": {
            "properties": [
                {key: prop.get(key) for key in financial_fields} for prop in properties
            ],
            "portfolio": {
                key: (payload.get("portfolio") or {}).get(key)
                for key in ("total_current_rent", "total_noi")
            },
        },
        "pricing_decision": {
            "properties": [
                {key: prop.get(key) for key in pricing_fields} for prop in properties
            ],
            "portfolio": {
                key: (payload.get("portfolio") or {}).get(key)
                for key in ("portfolio_price", "portfolio_price_basis")
            },
        },
        "sale_comps": [
            {"closed": prop.get("sale_comps"), "on_market": prop.get("active_comps")}
            for prop in properties
        ],
        "rent_comps": [prop.get("rent_comps") for prop in properties],
        "copy": {
            "track_record": payload.get("track_record"),
            "marketing": payload.get("marketing"),
            "properties": [{key: prop.get(key) for key in copy_fields} for prop in properties],
        },
        "media_selection": {
            "cover_hero": (payload.get("meta") or {}).get("hero"),
            "properties": [
                {"hero": prop.get("hero"), "gallery": prop.get("gallery")}
                for prop in properties
            ],
        },
        "regulatory_conclusions": payload.get("regulatory_conclusions"),
    }


def presentation_hashes(payload: dict) -> dict[str, str]:
    return {
        name: canonical_sha256(value)
        for name, value in presentation_projections(payload).items()
    }


def published_manifest(media: dict) -> dict:
    """Return the deal-repo instance: published selection only, no rejected research."""
    published_ids = set(media["published"]["order"])
    return {
        "schema_version": media["schema_version"],
        "deal_id": media["deal_id"],
        "archive": [item for item in media["archive"] if item["id"] in published_ids],
        "published": media["published"],
    }


#: The canonical headshot registry. A team member's headshot is RESOLVED from
#: this registry by the member's name, never supplied as a file path: the live
#: Yarmouth site shipped a 240x240 unregistered thumbnail hand-copied from
#: another deal's workspace because the contract accepted any path that existed
#: on disk (RC-7).
_REGISTRY_RELATIVE = Path("branding") / "headshots" / "canonical-headshots.json"
_headshot_registry_cache: tuple[Path, dict] | None = None


def _headshot_registry() -> tuple[Path, dict]:
    """Return (repo root, registry) by SEARCHING upward for the registry file.

    This module runs from two depths below the repository root: canonically
    from `reporting/bovsite/`, and from the laaa-core plugin's vendored copy at
    `plugins/laaa-core/vendor/bovsite/`. A fixed `parents[N]` is therefore
    wrong for one of them -- `parents[2]` resolves the vendored copy to
    `plugins/laaa-core/`, whose `branding/` does not exist, so every build
    through the vendored generator raised before staging a headshot.
    """
    global _headshot_registry_cache
    if _headshot_registry_cache is None:
        for parent in Path(__file__).resolve().parents:
            candidate = parent / _REGISTRY_RELATIVE
            if candidate.is_file():
                _headshot_registry_cache = (
                    parent, json.loads(candidate.read_text(encoding="utf-8")))
                break
        else:
            raise PayloadError(
                f"headshot registry not found: no {_REGISTRY_RELATIVE.as_posix()} "
                f"in any parent of {Path(__file__).resolve().parent}. Headshots "
                "resolve from that registry; run the build from the "
                "LAAA-AI-Prompts checkout.")
    return _headshot_registry_cache


def resolve_headshot(name: str) -> dict:
    """Resolve a team member's approved headshot from the canonical registry.

    Matches the member's name against each registry person's name and aliases
    (whitespace- and case-insensitive). Fails loudly on no-match or ambiguity;
    never falls through to a supplied file. The BOV renders the canonical
    square master per the registry's selection rule.
    """
    repo_root, registry = _headshot_registry()
    people = registry["people"]
    wanted = " ".join(str(name or "").split()).casefold()
    matches = []
    for key, person in people.items():
        candidates = [person.get("name", "")] + list(person.get("aliases") or [])
        if any(" ".join(str(c).split()).casefold() == wanted for c in candidates):
            matches.append((key, person))
    if not matches:
        known = ", ".join(sorted(p.get("name", k) for k, p in people.items()))
        raise PayloadError(
            f"no approved headshot for team member {name!r} in "
            f"branding/headshots/canonical-headshots.json. Registered people: "
            f"{known}. Fix the member's name or register the person; never "
            "supply a headshot file path.")
    if len(matches) > 1:
        keys = ", ".join(key for key, _ in matches)
        raise PayloadError(
            f"team member {name!r} matches more than one headshot registry "
            f"entry ({keys}); fix the registry aliases before building.")
    key, person = matches[0]
    derivative = person.get("canonicalSquare") or {}
    rel = derivative.get("path")
    sha = derivative.get("sha256")
    source = repo_root / rel if rel else None
    if not rel or not sha or not source.is_file():
        raise PayloadError(
            f"{name}: registry entry {key!r} has no usable canonicalSquare "
            f"master on disk ({rel}). Restore the canonical file; never "
            "substitute another image.")
    if hashlib.sha256(source.read_bytes()).hexdigest() != sha:
        raise PayloadError(
            f"{name}: canonical headshot master {rel} does not match its "
            "registered sha256. Restore the canonical file; never edit or "
            "replace a registered derivative in place.")
    return {"key": key, "source": source, "sha256": sha,
            "filename": f"team-{key}{Path(rel).suffix}"}


def _resolved_team(team: dict) -> dict:
    """Bind each team member to their registered headshot in the payload."""
    resolved = deepcopy(team)
    for member in (resolved.get("leads") or []) + (resolved.get("grid") or []):
        binding = resolve_headshot(member.get("name", ""))
        member["headshot"] = f"images/{binding['filename']}"
        member["headshot_sha256"] = binding["sha256"]
    return resolved


def _stage_headshots(payload: dict, site_repo: Path) -> None:
    """Copy each member's registered derivative to its generated filename."""
    team = payload.get("team") or {}
    for member in (team.get("leads") or []) + (team.get("grid") or []):
        binding = resolve_headshot(member.get("name", ""))
        shutil.copy2(binding["source"], site_repo / "images" / binding["filename"])


def _remove_stale_managed_images(site_repo: Path, keep: set[str]) -> None:
    """Remove only photos named by the prior generated media manifest."""
    previous_path = site_repo / "media-manifest.json"
    if not previous_path.is_file():
        return
    try:
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    images = (site_repo / "images").resolve()
    for item in previous.get("archive") or []:
        filename = item.get("filename") if isinstance(item, dict) else None
        if not isinstance(filename, str) or Path(filename).name != filename or filename in keep:
            continue
        candidate = (images / filename).resolve()
        try:
            candidate.relative_to(images)
        except ValueError:
            continue
        if candidate.is_file():
            candidate.unlink()


def build_payload(workspace: DealWorkspace) -> dict:
    """Build one deal payload, reporting every copy problem together."""
    with _collect_copy_problems():
        return _build_payload(workspace)


def _build_payload(workspace: DealWorkspace) -> dict:
    deal = workspace.load("deal")
    financials = workspace.load("financials")
    pricing = workspace.load("pricing")
    sale = workspace.load("comps-sale")
    rent = workspace.load("comps-rent")
    copy = workspace.load("copy")
    media = workspace.load("media-manifest")
    approved_contract = project_domains(workspace)
    claim_errors = publication_errors(copy)
    if claim_errors:
        raise PayloadError("client-facing copy is blocked: " + "; ".join(claim_errors))

    presentation = deal["presentation"]
    fin_presentation = financials.get("presentation") or {}
    # A required section must be satisfiable by declaring "not applicable"
    # rather than by inventing numbers. Whether a BOV is all cash, new debt,
    # assumed debt or seller financed is a DEAL INPUT (Glen). This used to
    # hard-raise unless financing, loan_payments and principal_reduction were
    # all present, so removing an unsourced financing block was impossible and
    # the entire Proposed Financing panel on the first real BOV was invented.
    structure = fin_presentation.get("financing_structure")
    required_financial_presentation = ["unit_mix", "expense_notes", "tax_rate", "financing_structure"]
    if structure != "all_cash":
        required_financial_presentation += ["financing", "loan_payments", "principal_reduction"]
    missing = [key for key in required_financial_presentation if key not in fin_presentation]
    if missing:
        raise PayloadError(
            "financials.presentation is missing explicit site fields: " + ", ".join(missing)
        )
    unit_sf_by_type = {item["unit_type"]: item for item in deal["unit_sf"]}
    unit_mix = deepcopy(fin_presentation["unit_mix"])
    for row in unit_mix:
        authority = unit_sf_by_type.get(row["type"])
        if authority is None or row["sf"] != authority["value"]:
            raise PayloadError(
                f"unit mix {row['type']!r} does not reconcile to deal.json unit_sf"
            )
        row["sf_basis"] = authority["sf_basis"]

    by_media_id = {item["id"]: item for item in media["archive"]}
    hero_id = media["published"]["hero"]
    if hero_id is None:
        fallback = presentation.get("no_photo_cover")
        if not fallback:
            raise PayloadError("no-photo approval requires presentation.no_photo_cover")
        hero_ref = fallback
    else:
        if hero_id not in by_media_id:
            raise PayloadError(f"published hero {hero_id!r} is absent from media archive")
        hero_ref = f"images/{by_media_id[hero_id]['filename']}"

    # A land deal carries no subject operating statement, so operations are null.
    # Substitute an explicit zero rather than special-casing forty lines of
    # arithmetic below: every derived figure then computes to 0, and the
    # Financial Analysis section omits the operating statement entirely rather
    # than printing those zeros. Nothing false is published, because nothing
    # derived from these is rendered on a land deal.
    ZERO_OPERATION = {
        "gross_scheduled_rent": 0, "vacancy": 0, "credit_loss": 0,
        "other_income": 0, "operating_expenses": 0, "noi": 0,
    }
    current = pricing["operations"]["current"] or ZERO_OPERATION
    market = (pricing["operations"].get("proforma")
              or pricing["operations"]["normalized"] or ZERO_OPERATION)
    units = deal["unit_count"]["value"]
    # Absent means multifamily, so every spine written before deal_type existed
    # keeps its exact current behaviour.
    is_land = deal.get("deal_type", "multifamily") == "land"
    building_sf = deal["building_sf"]["value"]
    decision = pricing["decision"]
    price = decision["exact_price"]
    current_gross = current["gross_scheduled_rent"] + current["other_income"]
    market_gross = market["gross_scheduled_rent"] + market["other_income"]
    current_goi = current_gross - current["vacancy"] - current["credit_loss"]
    market_goi = market_gross - market["vacancy"] - market["credit_loss"]
    if structure == "all_cash":
        # No debt: zero service, zero paydown, and the whole price is equity.
        # The arithmetic below is then correct without a special case, and the
        # renderer drops the debt rows rather than printing rows of $0.
        financing_public = None
        loan_payments = [0.0, 0.0]
        principal = [0.0, 0.0]
        down_payment = price
    else:
        financing = fin_presentation["financing"]
        # support_ref and approval_exception_id are internal provenance. The
        # BASIS travels to the page (an illustrative figure must be labelled);
        # the reference behind it stays in the workspace.
        financing_public = {
            key: financing[key]
            for key in ("loan_amount", "rate", "amortization", "dcr", "basis")
        }
        loan_payments = fin_presentation["loan_payments"]
        principal = fin_presentation["principal_reduction"]
        if len(loan_payments) != 2 or len(principal) != 2:
            raise PayloadError("loan_payments and principal_reduction must each contain two values")
        down_payment = price - financing["loan_amount"]
    pretax = [current["noi"] + loan_payments[0], market["noi"] + loan_payments[1]]
    total_return = [pretax[0] + principal[0], pretax[1] + principal[1]]
    return_denominator = down_payment if down_payment > 0 else price
    expense_rows, expense_notes = _expense_rows(financials, current, market)
    # Income lines carry the same basis discipline (Glen, 2026-08-09). They
    # share the operating statement's ONE note registry, so a reader sees a
    # single numbered list under the whole statement rather than two.
    ledger_lines = client_basis_by_line(financials)
    income_notes = {}
    for key, label in INCOME_DECISION_LINES:
        if key == "credit_loss" and not (
                current.get("credit_loss") or market.get("credit_loss")):
            # A 0% credit loss is OMITTED from the page entirely (Glen,
            # 2026-08-09; see the row-omission rule below), so it must not
            # carry a note either: a numbered note explaining a row nobody
            # sees is a dangling entry in the list.
            continue
        declared = ledger_lines.get(key) or ledger_lines.get(label.strip().casefold())
        if declared:
            # expense_notes IS the registry and grows as each marker is
            # assigned, so counting income_notes as well double-counts and
            # leaves gaps: 13, 15, 17. Numbered lists with holes read as
            # missing content, which is the defect this whole section exists
            # to remove.
            marker = _next_marker(expense_notes)
            expense_notes[marker] = [label, declared]
            income_notes[key] = marker

    # One anchor decides every published cap on the page: the headline
    # cap_current / cap_market below and the opinion-of-value ladder are the
    # same NOI over prices, so an unanchored NOI makes all of them wrong by the
    # same constant. Keyed exactly like publication itself: decision.cap_rate
    # null claims no reassessment anywhere and needs no anchor; the zero-rate
    # case keeps its own specific refusal in schema.py rather than a confusing
    # identity failure here.
    tax_rate = fin_presentation.get("tax_rate")
    tax_anchor = fin_presentation.get("tax_anchor")
    if decision.get("cap_rate") is not None and tax_rate:
        anchor_problems = tax_anchor_problems(
            price, tax_rate, tax_anchor, expense_rows,
            modeled_totals=(current["operating_expenses"],
                            market["operating_expenses"]),
        )
        if anchor_problems:
            raise PayloadError(
                "the NOI is not anchored, so no cap may publish: "
                + " ".join(anchor_problems)
            )

    sale_rows = []
    active_rows = []
    sale_sources = []
    active_sources = []
    excluded_sale_ids = set(sale["conclusions"]["excluded_ids"])
    for comp in sale["rows"]:
        if comp["quality_rating"] == "exclude" or comp["id"] in excluded_sale_ids:
            continue
        rendered = {
            "address": comp["address"],
            "status": comp["status"].title(),
            "price": comp["sale_price"],
            "units": comp["units"],
            "year_built": comp.get("year_built"),
            "building_sf": comp["building_sf"],
            "lot_sf": comp.get("lot_sf"),
            "date": comp.get("sale_date") or comp.get("as_of_date") or comp["source"]["verified_at"],
            "days_on_market": comp.get("days_on_market"),
            "price_per_unit": comp["price_per_unit"],
            "price_per_sf": comp["price_per_sf"],
            # DERIVED, never authored: two recorded figures divided. Land trades
            # on price per land SF, and deriving it here means it cannot drift
            # from the sale price and lot size printed beside it in the table.
            "price_per_land_sf": (
                comp["sale_price"] / comp["lot_sf"]
                if comp.get("lot_sf") and comp.get("sale_price") else None
            ),
            "grm": comp["grm"],
            "cap_rate": _fraction_to_percent(comp["cap_rate"]),
            "image": comp.get("image"),
            "restatement": comp.get("restatement"),
            "summary": comp["weight_reason"],
            "relevance": "; ".join(comp["physical_differences"]) or "Physical comparison recorded.",
            "considerations": "; ".join(comp["operational_differences"]) or "Operating comparison recorded.",
        }
        if comp["status"] == "closed":
            sale_rows.append(rendered)
            sale_sources.append(comp)
        else:
            active_rows.append(rendered)
            active_sources.append(comp)

    # Map pin numbers, one per DISTINCT location in row order, matching
    # maps.pins_for exactly. Without this a reader cannot tell which table row
    # belongs to which pin, and co-located rows made the map look like it had
    # dropped comps entirely.
    # Keyed on the ROUNDED COORDINATE, exactly as maps.pins_for dedupes, and
    # walked in the same row order the manifest is built from. Numbering these
    # by address instead would be a second, independent rule: two rows sharing
    # an address but not a pin, or a manifest ordered differently from the
    # table, would print a number that points at the wrong pin, which is worse
    # than the missing numbers this replaced.
    def _manifest_keys(category):
        """Distinct pin coordinates for a category, in maps.pins_for's order."""
        path = workspace.site_repo / "map-manifest.json"
        if not path.is_file():
            return None
        entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
        mine = [e for e in entries
                if e.get("property") == deal["slug"] and e.get("category") == category]
        out = []
        for entry in sorted(mine, key=lambda x: x.get("order", 0)):
            key = (round(entry["lat"], 6), round(entry["lng"], 6))
            if key not in out:
                out.append(key)
        return out

    def _assign_map_refs(rows, sources, category):
        seen = {}
        for row, source in zip(rows, sources):
            pin = source.get("pin") or {}
            key = (round(pin.get("latitude", 0.0), 6),
                   round(pin.get("longitude", 0.0), 6))
            if key not in seen:
                seen[key] = len(seen) + 1
            row["map_ref"] = seen[key]
        # The printed number is only trustworthy if the manifest the PNG was
        # rendered from yields the SAME distinct locations in the SAME order.
        # A manifest ordered differently, or one left stale after the comps
        # changed, would otherwise print a number pointing at another pin, and
        # a certified PNG digest cannot catch that because the digest still
        # matches the bytes that were rendered. Refuse instead.
        expected = _manifest_keys(category)
        if expected is not None and expected:
            derived = list(seen.keys())
            if derived != expected:
                raise PayloadError(
                    f"{category} comp rows and map-manifest.json disagree on pin "
                    f"order or location: rows give {derived}, the manifest gives "
                    f"{expected}. The printed row numbers would point at the "
                    f"wrong pins. Re-render the maps from the current manifest "
                    f"(maps.py --force) so both derive from the same pins."
                )
        return rows

    rent_rows = [
        {
            "address": comp["address"],
            "unit_type": comp["unit_type"],
            "rent": comp["monthly_rent"],
            "square_feet": comp["unit_sf"],
            "square_feet_basis": comp["unit_sf_basis"],
            "distance": comp["distance_miles"],
            "image": comp.get("image"),
        }
        for comp in rent["rows"]
        if comp["quality_rating"] != "exclude"
    ]
    _assign_map_refs(rent_rows, [
        comp for comp in rent["rows"] if comp["quality_rating"] != "exclude"], "rent")
    _assign_map_refs(sale_rows, sale_sources, "sold")
    _assign_map_refs(active_rows, active_sources, "active")

    gallery = [
        {
            "src": f"images/{by_media_id[item_id]['filename']}",
            "alt": by_media_id[item_id]["category"].replace("_", " ").title(),
        }
        for item_id in media["published"]["order"]
        if item_id != hero_id
    ]
    maps = presentation.get("maps") or {}
    property_payload = {
        "slug": deal["slug"],
        "short_name": presentation["short_name"],
        "show_active_listings": bool(presentation.get("show_active_listings")),
        "show_local_closings": presentation.get("show_local_closings", True) is not False,
        "show_buyout_model": bool(presentation.get("show_buyout_model")),
        "buyout_model": presentation.get("buyout_model"),
        "show_value_scenarios": bool(presentation.get("show_value_scenarios")),
        "value_scenarios": presentation.get("value_scenarios"),
        "address": deal["primary_address"],
        "city": presentation["city"],
        "submarket": presentation["submarket"],
        "track_record_submarkets": presentation.get("track_record_submarkets"),
        "apn": ", ".join(deal["apns"]),
        "units": units,
        "building_sf": building_sf,
        "lot_sf": deal["lot"]["square_feet"],
        "lot_acres": deal["lot"].get("acres") or deal["lot"]["square_feet"] / 43560,
        "year_built": deal["year_built"]["value"],
        "renovated_year": (deal.get("renovated_year") or {}).get("value"),
        "parking": presentation["parking"],
        "hero": hero_ref,
        "price": price,
        "price_per_unit": decision["price_per_unit"],
        "price_per_sf": decision.get("price_per_sf") or price / building_sf,
        "value_range": _money_range(decision["range_low"], decision["range_high"]),
        "opinion_of_value": _opinion_of_value(
            decision, current, market, fin_presentation.get("tax_rate"),
            deal["unit_count"]["value"], building_sf,
            # Land metrics are passed ONLY on a land deal. An apartment building
            # sits on a lot too, so passing these unconditionally would silently
            # add a $/Land SF column to every BOV already in the field.
            lot_sf=(deal["lot"]["square_feet"] if is_land else None),
            buildable_units=(presentation.get("buildable_units") if is_land else None),
        ),
        "scheduled_rent": [current["gross_scheduled_rent"] / 12, market["gross_scheduled_rent"] / 12],
        "monthly_sgi": [current_gross / 12, market_gross / 12],
        "additional_income": [current["other_income"] / 12, market["other_income"] / 12],
        # A land deal can carry rent (this church leases to schools) or none at
        # all. Either way a GRM against a parcel price is not the metric anyone
        # buys on, and a zero denominator is a crash, so it is withheld rather
        # than forced. Guarded on the denominator, not on deal_type, so a genuine
        # zero-income apartment building is caught by the same line.
        "grm_current": (price / current["gross_scheduled_rent"]
                        if current["gross_scheduled_rent"] else None),
        "grm_market": (price / market["gross_scheduled_rent"]
                       if market["gross_scheduled_rent"] else None),
        # The subject cap is a LAAA calculation (NOI / recommended value) and
        # publishes by default per the Camarillo design lock. decision.cap_rate is
        # the analyst's per-deal publish control: when it is null the cap is
        # withheld and both cap figures pass through as None so the Financial
        # Analysis section omits the rows. The basis labels are fixed structured
        # strings, NEVER decision.cap_rate_basis, which is an internal analyst note
        # that must never reach the public page.
        "cap_current": None if decision.get("cap_rate") is None else current["noi"] / price * 100,
        "cap_market": None if decision.get("cap_rate") is None else market["noi"] / price * 100,
        "cap_current_basis": "LAAA calculation: current NOI / recommended value",
        "cap_market_basis": "LAAA calculation: market NOI / recommended value",
        "noi_current": current["noi"],
        "noi_market": market["noi"],
        "tax_rate": fin_presentation["tax_rate"],
        "tax_anchor": deepcopy(tax_anchor),
        "expense_total": current["operating_expenses"],
        "expense_per_unit": current["operating_expenses"] / units if units else None,
        "expense_per_sf": current["operating_expenses"] / building_sf,
        "unit_mix": unit_mix,
        "operating": {
            "sgi": [current_gross, market_gross],
            "vacancy": [current["vacancy"] + current["credit_loss"], market["vacancy"] + market["credit_loss"]],
            "vacancy_pct": (current["vacancy"] + current["credit_loss"]) / current_gross * 100 if current_gross else 0,
            "vacancy_only": [current["vacancy"], market["vacancy"]],
            "vacancy_only_pct": current["vacancy"] / current_gross * 100 if current_gross else 0,
            "credit_loss": [current["credit_loss"], market["credit_loss"]],
            "credit_loss_pct": current["credit_loss"] / current_gross * 100 if current_gross else 0,
            "goi": [current_goi, market_goi],
            "expenses": [current["operating_expenses"], market["operating_expenses"]],
            "expense_ratio": [
                current["operating_expenses"] / current_goi * 100 if current_goi else 0,
                market["operating_expenses"] / market_goi * 100 if market_goi else 0,
            ],
            "noi": [current["noi"], market["noi"]],
            "loan_payments": loan_payments,
            "pretax_cf": pretax,
            "pretax_cf_pct": [value / return_denominator * 100 for value in pretax],
            "principal_reduction": principal,
            "total_return": total_return,
            "total_return_pct": [value / return_denominator * 100 for value in total_return],
        },
        "expense_lines": expense_rows,
        "expense_notes": expense_notes,
        "income_notes": income_notes,
        "financing_structure": structure,
        "financing": financing_public,
        "highlights": _paragraphs(copy, "investment", "highlights"),
        "overview": _paragraphs(copy, "investment", "overview"),
        "location_title": _one(copy, "location", "title"),
        "location_narrative": _paragraphs(copy, "location", "narrative"),
        "physical_narrative": _paragraphs(copy, "property_details", "physical_narrative"),
        "market_narrative": _paragraphs(copy, "buyer_profile", "market_narrative"),
        "positioning_narrative": _paragraphs(copy, "buyer_profile", "positioning_narrative"),
        "rent_narrative": _paragraphs(copy, "rent_comps", "narrative"),
        "valuation_narrative": _paragraphs(copy, "financial_analysis", "valuation_narrative"),
        "strategy": _paired(copy, "marketing", "strategy_titles", "strategy_copy"),
        "buyer_profiles": _paired(copy, "buyer_profile", "profile_titles", "profile_copy"),
        "disclosures": _paragraphs(copy, "financial_analysis", "disclosures"),
        "gallery": gallery,
        "maps": {
            "subject": maps.get("subject"),
            "sale": maps.get("sale"),
            "rent": maps.get("rent"),
            "active": maps.get("active"),
        },
        "rent_comps": rent_rows,
        "sale_comps": sale_rows,
        "active_comps": active_rows,
        "map_points": [],
        "track_record": {},
    }
    _renumber_notes_in_reading_order(property_payload)
    payload = {
        "schema_version": 3,
        "document_type": "bov",
        "site_mode": "single",
        "meta": {
            "domain": presentation["domain"],
            "client": presentation["client"],
            "title": presentation["title"],
            "subtitle": presentation["subtitle"],
            "month_year": presentation["month_year"],
            "cover_label": presentation.get("cover_label", "Confidential Broker Opinion of Value"),
            "noindex": bool(presentation.get("noindex")),
            "hero": hero_ref,
            "hero_tall": presentation.get("hero_tall"),
        },
        "team": _resolved_team(presentation["team"]),
        "track_record": {
            "metrics": [["{closed}", "Closed Transactions"], ["{volume}", "Total Sales Volume"], ["{apt_units}", "Apartment Units Sold"]],
            "narrative": _paragraphs(copy, "track_record", "narrative"),
            "achievements": _achievement_pairs(
                _paragraphs(copy, "track_record", "achievements", required=False)
            ),
            "press": _paragraphs(copy, "track_record", "press", required=False),
        },
        "marketing": {
            "metrics": [["{subscribers}", "Active Email Subscribers"]],
            "channels": [
                [item["title"], item["copy"]]
                for item in _paired(copy, "marketing", "channel_titles", "channel_copy")
            ],
            "narrative": _paragraphs(copy, "marketing", "narrative"),
            # Both optional, both were hardcoded in blocks.marketing. The pull
            # quote and the three difference blocks are the most deal-specific
            # marketing copy on the page (East Palmdale names the Antelope
            # Valley buyer pool and the 1031 buyers who trade that size class),
            # and a literal in the renderer cannot say any of that. Absent from
            # the spine, blocks.marketing falls back to the locked defaults, so
            # every existing deal renders exactly as before.
            "pull_quote": (_paragraphs(copy, "marketing", "pull_quote", required=False) or [None])[0],
            "differences": _paired(copy, "marketing", "difference_titles",
                                   "difference_copy", required=False),
            "firm_ranking": presentation.get("firm_ranking"),
        },
        "properties": [property_payload],
        "regulatory_conclusions": deal["regulatory_conclusions"],
        "copy_contract": {
            "document": approved_contract["copy"],
        },
        "approval_contract": {
            "copy": approved_contract["copy"],
            "media_selection": approved_contract["media_selection"],
            "regulatory_conclusions": approved_contract["regulatory_conclusions"],
        },
    }
    payload["approval_binding"] = {
        "deal_id": deal["deal_id"],
        "domains": domain_hashes(workspace),
        "presentation_hashes": presentation_hashes(payload),
    }
    return payload


def write_payload(workspace: DealWorkspace, site_repo: Path | None = None) -> Path:
    payload = build_payload(workspace)
    site_repo = site_repo or workspace.site_repo
    if not site_repo.is_dir():
        raise PayloadError(f"configured site repo does not exist: {site_repo}")
    images_dir = site_repo / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    media = workspace.load("media-manifest")
    by_id = {item["id"]: item for item in media["archive"]}
    keep = {by_id[item_id]["filename"] for item_id in media["published"]["order"]}
    _remove_stale_managed_images(site_repo, keep)
    for item_id in media["published"]["order"]:
        item = by_id[item_id]
        shutil.copy2(
            workspace.media_root / "published" / item["filename"],
            images_dir / item["filename"],
        )
    _stage_headshots(payload, site_repo)
    write_json_atomic(site_repo / "media-manifest.json", published_manifest(media))
    destination = site_repo / "bov-site.json"
    write_json_atomic(destination, payload)
    return destination


def _portfolio_members(workspace: DealWorkspace) -> tuple[dict, dict[str, DealWorkspace]]:
    portfolio = workspace.load("portfolio")
    members: dict[str, DealWorkspace] = {}
    for item in portfolio["members"]:
        member = DealWorkspace.from_path(
            workspace.resolve_relative(item["workspace"], label=f"portfolio member {item['deal_id']}")
        )
        actual = member.load("deal")["deal_id"]
        if actual != item["deal_id"]:
            raise PayloadError(f"portfolio member {item['deal_id']!r} points to {actual!r}")
        members[actual] = member
    return portfolio, members


def build_portfolio_payload(workspace: DealWorkspace) -> tuple[dict, dict, dict[str, str]]:
    """Build the portfolio payload, batching copy problems across members."""
    with _collect_copy_problems():
        return _build_portfolio_payload(workspace)


def _build_portfolio_payload(workspace: DealWorkspace) -> tuple[dict, dict, dict[str, str]]:
    portfolio, members = _portfolio_members(workspace)
    member_payloads = {deal_id: build_payload(member) for deal_id, member in members.items()}
    # Every member is built, so the collection is complete. Report it here:
    # the portfolio invariants below compare member copy for byte-identity,
    # and a placeholder left by a missing block would fail that comparison
    # first, replacing the actionable report with "must be byte-identical".
    _raise_collected_copy_problems()
    member_hashes = {deal_id: domain_hashes(member) for deal_id, member in members.items()}
    member_contracts = {deal_id: project_domains(member) for deal_id, member in members.items()}
    aggregate = portfolio_domain_hashes(portfolio, member_hashes)
    presentation = portfolio["presentation"]
    first_payload = next(iter(member_payloads.values()))
    for deal_id, payload in member_payloads.items():
        if payload["track_record"] != first_payload["track_record"] or payload["marketing"] != first_payload["marketing"]:
            raise PayloadError(
                f"{deal_id}: portfolio-wide Track Record and Marketing copy must be byte-identical"
            )

    combined_items = []
    combined_order = []
    filename_map: dict[tuple[str, str], str] = {}
    portfolio_contract_members = {}
    hero_combined_id = None
    for deal_id, member in members.items():
        deal = member.load("deal")
        media = member.load("media-manifest")
        by_id = {item["id"]: item for item in media["archive"]}
        for item_id in media["published"]["order"]:
            item = copy.deepcopy(by_id[item_id])
            combined_id = f"{deal_id}:{item_id}"
            filename = f"{deal['slug']}-{item['filename']}"
            filename_map[(deal_id, item_id)] = filename
            item["id"] = combined_id
            item["filename"] = filename
            if item.get("exception_id"):
                item["exception_id"] = f"{deal_id}:{item['exception_id']}"
            combined_items.append(item)
            combined_order.append(combined_id)
            if deal_id == presentation["hero_member"] and item_id == presentation["hero_media_id"]:
                hero_combined_id = combined_id
        portfolio_contract_members[deal_id] = {
            "copy": member_contracts[deal_id]["copy"],
            "media_selection": member_contracts[deal_id]["media_selection"],
            "regulatory_conclusions": member_contracts[deal_id]["regulatory_conclusions"],
        }
    if hero_combined_id is None:
        raise PayloadError("portfolio presentation hero does not resolve to published member media")
    combined_media = {
        "schema_version": "1.0.0",
        "deal_id": portfolio["portfolio_id"],
        "archive": combined_items,
        "published": {"order": combined_order, "hero": hero_combined_id, "contact_sheet_sha256": None},
    }

    properties = []
    for deal_id, member_payload in member_payloads.items():
        prop = copy.deepcopy(member_payload["properties"][0])
        prop["deal_id"] = deal_id
        member_media = members[deal_id].load("media-manifest")
        by_id = {item["id"]: item for item in member_media["archive"]}
        hero_id = member_media["published"]["hero"]
        if hero_id:
            prop["hero"] = f"images/{filename_map[(deal_id, hero_id)]}"
        prop["gallery"] = [
            {
                "src": f"images/{filename_map[(deal_id, item_id)]}",
                "alt": by_id[item_id]["category"].replace("_", " ").title(),
            }
            for item_id in member_media["published"]["order"]
            if item_id != hero_id
        ]
        prop["card_image"] = prop["hero"]
        properties.append(prop)

    payload = {
        "schema_version": 3,
        "document_type": "bov",
        "site_mode": "portfolio",
        "meta": {
            "domain": presentation["domain"],
            "client": presentation["client"],
            "title": portfolio["name"],
            "subtitle": presentation["subtitle"],
            "month_year": presentation["month_year"],
            "cover_label": presentation.get("cover_label", "Confidential Broker Opinion of Value"),
            # The generator deterministically composes the portfolio cover from
            # each member's approved hero. Bind the stable generated paths now
            # so rendering the approved inputs does not look like a post-build
            # presentation mutation to the site approval gate.
            "hero": "images/portfolio-hero-split.jpg",
            "hero_tall": "images/portfolio-hero-split-tall.jpg",
            "portfolio_map": presentation["portfolio_map"],
        },
        "team": _resolved_team(presentation["team"]),
        "track_record": first_payload["track_record"],
        "marketing": first_payload["marketing"],
        "portfolio": {
            "total_units": sum(prop["units"] for prop in properties),
            "total_current_rent": sum(prop["scheduled_rent"][0] * 12 for prop in properties),
            "total_noi": sum(prop["noi_current"] for prop in properties),
            "portfolio_price": portfolio["portfolio_price"],
            "portfolio_price_basis": portfolio["portfolio_price_basis"],
        },
        "properties": properties,
        "regulatory_conclusions": {
            deal_id: member.load("deal")["regulatory_conclusions"]
            for deal_id, member in members.items()
        },
        "copy_contract": {"members": {deal_id: value["copy"] for deal_id, value in portfolio_contract_members.items()}},
        "portfolio_contract": {"members": portfolio_contract_members},
    }
    payload["approval_binding"] = {
        "deal_id": portfolio["portfolio_id"],
        "domains": aggregate,
        "members": member_hashes,
        "presentation_hashes": presentation_hashes(payload),
    }
    return payload, combined_media, filename_map


def write_portfolio_payload(workspace: DealWorkspace, site_repo: Path | None = None) -> Path:
    portfolio, members = _portfolio_members(workspace)
    site_repo = site_repo or workspace.resolve_relative(portfolio["site_repo"], label="portfolio.site_repo")
    if not site_repo.is_dir():
        raise PayloadError(f"configured portfolio site repo does not exist: {site_repo}")
    payload, combined_media, filename_map = build_portfolio_payload(workspace)
    images = site_repo / "images"
    images.mkdir(parents=True, exist_ok=True)
    _remove_stale_managed_images(
        site_repo, {item["filename"] for item in combined_media["archive"]}
    )
    for deal_id, member in members.items():
        manifest = member.load("media-manifest")
        by_id = {item["id"]: item for item in manifest["archive"]}
        for item_id in manifest["published"]["order"]:
            shutil.copy2(
                member.media_root / "published" / by_id[item_id]["filename"],
                images / filename_map[(deal_id, item_id)],
            )
    _stage_headshots(payload, site_repo)
    write_json_atomic(site_repo / "media-manifest.json", combined_media)
    destination = site_repo / "bov-site.json"
    write_json_atomic(destination, payload)
    return destination
