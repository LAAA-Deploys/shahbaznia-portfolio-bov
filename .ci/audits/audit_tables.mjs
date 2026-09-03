/**
 * Table fit audit: is every cell, row and column deliberately sized?
 *
 * A data table is the densest thing on a BOV and the easiest to get wrong. This
 * measures every table on every page at every breakpoint and reports:
 *
 *   WRAP        a cell renders on more than one line (Glen: no wrapping)
 *   HEADER_WRAP a column header splits across lines ("BLDG SF" on two rows)
 *   RAGGED      row heights inside one table are not uniform
 *   LOPSIDED    one column is disproportionately wider than the rest
 *   TINY        font renders below the legibility floor
 *   CONTRAST    text fails WCAG AA against its own background
 *   CLIPPED     the table overflows with no way to scroll to the rest
 *   MISALIGNED  a numeric column is not right-aligned
 *   NO_SUMMARY  a table has numeric columns but no summary row at all
 *   SUMMARY_GAP a summary row dashes out a column that has numbers above it
 *   SUMMED_RATE a rate column was totalled instead of averaged
 *
 * The last three are the "every data table ends with a real summary" rule.
 * The sale-comps table shipped medians for price, $/unit and $/SF while printing
 * a dash under GRM and Cap, though five of six comps carried both. Those are two
 * of the four metrics the pricing doctrine holds to equal standing, so the table
 * quietly dropped the yield case. The same class was then found in the rent roll.
 *
 * A table counts as a data grid by SHAPE, not by markup: a column is
 * aggregatable when at least 2 body cells parse as the SAME kind of number.
 * Keying off the .table-scroll wrapper had silently excluded the operating-data
 * and expense tables, which are not inside a scroller.
 *
 * SUMMED_RATE exists because the opposite mistake is just as bad and looks
 * authoritative: totalling $/unit, $/SF, cap or GRM yields a meaningless figure.
 * Rates take a median or a weighted average; only true quantities get summed.
 *
 * Desktop and mobile are judged separately, because a table that reads well at
 * 1440 can be unreadable at 390 and the fix differs.
 *
 * Usage: node audit_tables.mjs [baseUrl]
 */
import { chromium } from 'playwright';
import { discoverPages, assertRendered } from './audit_pages.mjs';

const BASE = process.argv[2] || 'http://localhost:8901';
const PAGES = discoverPages();   // every staged route, never a hardcoded list
const WIDTHS = [390, 768, 1440];

const LIMITS = {
  minFontPx: 9,          // below this a figure is not readable on a phone
  rowHeightTolerance: 0.30,  // spread allowed between the tallest and shortest body row
  columnRatio: 6.0,      // widest data column vs narrowest, before it looks lopsided
  contrastNormal: 4.5,   // WCAG AA
  contrastLarge: 3.0,
};

const audit = (L) => {
  const out = [];

  const srgb = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  const lum = ([r, g, b]) => 0.2126 * srgb(r) + 0.7152 * srgb(g) + 0.0722 * srgb(b);
  const parse = (s) => (s.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
  const bgOf = (el) => {
    for (let n = el; n && n !== document.documentElement; n = n.parentElement) {
      const bg = getComputedStyle(n).backgroundColor;
      const p = parse(bg);
      if (p.length === 3 && !/rgba\(.*,\s*0\)/.test(bg)) return p;
    }
    return [255, 255, 255];
  };
  const ratio = (fg, bg) => {
    const a = lum(fg), b = lum(bg);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
  // a cell wraps if its content box is taller than roughly one line
  const lines = (el) => {
    const cs = getComputedStyle(el);
    let lh = parseFloat(cs.lineHeight);
    if (!lh || Number.isNaN(lh)) lh = parseFloat(cs.fontSize) * 1.2;
    const inner = el.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    return Math.max(1, Math.round(inner / lh));
  };

  document.querySelectorAll('table').forEach((table, ti) => {
    if (!table.offsetParent && getComputedStyle(table).display === 'none') return;
    const section = table.closest('[id]')?.id || `table${ti}`;
    const scroller = table.closest('.table-scroll');
    const isData = !!scroller;                    // .info-table prose may wrap
    const tag = `${section}${isData ? '' : ' (info)'}`;

    // clipped: wider than its parent with no scroll
    const parent = scroller || table.parentElement;
    if (table.scrollWidth > parent.clientWidth + 1) {
      const canScroll = getComputedStyle(parent).overflowX.match(/auto|scroll/);
      if (!canScroll) {
        out.push({ table: tag, issue: 'CLIPPED',
                   detail: `table is ${Math.round(table.scrollWidth)}px inside a ${Math.round(parent.clientWidth)}px box with no scroll` });
      }
    }

    const heads = [...table.querySelectorAll('thead th')];
    heads.forEach((th) => {
      if (isData && lines(th) > 1) {
        out.push({ table: tag, issue: 'HEADER_WRAP', detail: `header "${th.textContent.trim()}" wraps to ${lines(th)} lines` });
      }
    });

    const bodyRows = [...table.querySelectorAll('tbody tr')];
    bodyRows.forEach((tr) => {
      [...tr.children].forEach((td) => {
        const txt = td.textContent.trim();
        if (!txt) return;
        const n = lines(td);
        if (isData && n > 1) {
          out.push({ table: tag, issue: 'WRAP', detail: `"${txt.slice(0, 34)}" wraps to ${n} lines` });
        }
        const cs = getComputedStyle(td);
        const fs = parseFloat(cs.fontSize);
        if (fs < L.minFontPx) {
          out.push({ table: tag, issue: 'TINY', detail: `${fs.toFixed(1)}px on "${txt.slice(0, 22)}"` });
        }
        const c = ratio(parse(cs.color), bgOf(td));
        const need = (fs >= 18 || (fs >= 14 && +cs.fontWeight >= 700)) ? L.contrastLarge : L.contrastNormal;
        if (c < need) {
          out.push({ table: tag, issue: 'CONTRAST', detail: `${c.toFixed(2)}:1 needs ${need}:1 on "${txt.slice(0, 22)}"` });
        }
        // a cell holding only a figure must be right aligned
        if (isData && /^[($]?[\d,]+(\.\d+)?%?\)?$/.test(txt) && txt.length > 2 && cs.textAlign !== 'right') {
          out.push({ table: tag, issue: 'MISALIGNED', detail: `"${txt}" is ${cs.textAlign}, numbers align right` });
        }
      });
    });

    // ---- Aggregatable-column analysis -------------------------------------
    // A "data grid" is decided by SHAPE, not by markup. Keying off the
    // .table-scroll wrapper meant the operating-data and expense tables were
    // never checked, because they are not inside a scroller. A column is
    // aggregatable when at least 2 body cells parse as the SAME kind of number
    // (all currency, all plain, or all percent). That includes the expense
    // column but excludes a label/value column holding a year, an APN and a
    // sentence about parking.
    const kindOf = (s) => {
      const t = s.trim();
      if (!t || t === '-') return null;
      if (/^\(?\$[\d,]+(\.\d+)?\)?$/.test(t)) return 'money';
      if (/^-?[\d,]+(\.\d+)?%$/.test(t)) return 'pct';
      if (/^-?[\d,]+(\.\d+)?$/.test(t)) return 'num';
      return 'text';
    };
    const parseNum = (s) => {
      const n = parseFloat(String(s).replace(/[^0-9.\-]/g, ''));
      return Number.isFinite(n) ? (/^\(/.test(s.trim()) ? -n : n) : null;
    };
    const dataRowsAll = bodyRows.filter(r => !r.classList.contains('summary'));
    const width = Math.max(0, ...bodyRows.map(r => [...r.children].reduce((a, c) => a + (c.colSpan || 1), 0)));
    const colCells = (row) => { const m = []; let i = 0;
      for (const td of row.children) { for (let k = 0; k < (td.colSpan || 1); k++) m[i + k] = td; i += td.colSpan || 1; } return m; };
    const headText = [];
    { let i = 0;
      for (const th of heads) { const s = (th.colSpan || 1);
        for (let k = 0; k < s; k++) headText[i + k] = th.textContent.trim(); i += s; } }
    const aggCols = [];
    for (let ci = 0; ci < width; ci++) {
      const vals = dataRowsAll.map(r => (colCells(r)[ci] || {}).textContent || '').map(s => s.trim()).filter(Boolean);
      const kinds = vals.map(kindOf).filter(Boolean);
      const numeric = kinds.filter(k => k !== 'text');
      if (numeric.length >= 2 && numeric.length === kinds.length && new Set(numeric).size === 1) {
        aggCols.push({ ci, kind: numeric[0], header: headText[ci] || '', values: vals });
      }
    }

    // Every table with aggregatable columns needs a summary row at all.
    //
    // One shape legitimately has no aggregate: a scenario ladder, whose rows are
    // ALTERNATIVE values of the same asset rather than components of a whole.
    // Summing four candidate prices, or averaging four scenario cap rates,
    // produces a figure that is arithmetically fine and means nothing. Such a
    // table opts out explicitly with data-summary-exempt, and must say why, so
    // the exemption is a stated decision rather than a silent omission.
    const exempt = table.getAttribute('data-summary-exempt');
    const summaryRow = bodyRows.find(r => r.classList.contains('summary'));
    if (aggCols.length && !summaryRow && !exempt) {
      out.push({ table: tag, issue: 'NO_SUMMARY',
                 detail: `${aggCols.length} numeric column(s) and no summary row (${aggCols.map(c => c.header || '?').join(', ')})` });
    }

    // A rate must never be summed. Adding up $/unit, $/SF, cap rate or GRM
    // produces a meaningless figure that still looks authoritative.
    if (summaryRow) {
      const smap = colCells(summaryRow);
      for (const c of aggCols) {
        const shown = (smap[c.ci] || {}).textContent;
        if (!shown) continue;
        const v = parseNum(shown);
        if (v === null) continue;
        const isRate = /per\s|\/\s*unit|\/\s*sf|\bcap\b|\bgrm\b|rate|%|ratio|dcr/i.test(c.header) || c.kind === 'pct';
        if (!isRate) continue;
        const sum = c.values.map(parseNum).filter(n => n !== null).reduce((a, n) => a + n, 0);
        if (sum && Math.abs(v - sum) / Math.abs(sum) < 0.02) {
          out.push({ table: tag, issue: 'SUMMED_RATE',
                     detail: `"${c.header}" summary shows ${shown.trim()}, which equals the column SUM; a rate must be a median or weighted average` });
        }
      }
    }

    // A summary row must aggregate every column that carries numbers.
    // The sale-comps table published medians for price, $/unit and $/SF while
    // printing a dash under GRM and Cap, even though five of six comps had both.
    // Those are two of the four metrics the pricing doctrine holds to equal
    // standing, so dashing them quietly drops the yield case. Dates and text
    // columns are legitimately not aggregatable and are exempt.
    // Gap check now runs on ANY table with aggregatable columns, not only the
    // ones inside a scroller. Dates and text columns are exempt.
    if (summaryRow) {
      const smap = colCells(summaryRow);
      for (const c of aggCols) {
        if (/date|address|type|status|submarket|name/i.test(c.header)) continue;
        const cell = smap[c.ci];
        const shown = cell ? cell.textContent.trim() : '';
        // a cell covered by the label's colspan is a deliberate omission, not a gap
        const covered = cell && (cell.colSpan || 1) > 1;
        if (!covered && (shown === '-' || shown === '')) {
          out.push({ table: tag, issue: 'SUMMARY_GAP',
                     detail: `"${c.header || 'col' + (c.ci + 1)}" has ${c.values.length} values above it but the summary row shows "${shown || 'blank'}"` });
        }
      }
    }

    // uniform row heights: no wrapping means every body row should match
    const hs = !isData ? [] : bodyRows.filter(r => !r.classList.contains('summary'))
                       .map(r => r.getBoundingClientRect().height).filter(h => h > 4);
    // Two rows is enough to be ragged. This was > 2, which silently exempted
    // every short table, including the live sale-comps table that has exactly
    // two comps. Proved by A/B: the identical row-height defect went unreported
    // on the 2-row table and fired on the 5-row one.
    if (hs.length > 1) {
      const min = Math.min(...hs), max = Math.max(...hs);
      if (min > 0 && (max - min) / min > L.rowHeightTolerance) {
        out.push({ table: tag, issue: 'RAGGED', detail: `row heights run ${Math.round(min)}px to ${Math.round(max)}px` });
      }
    }

    // column balance across data columns
    if (isData && bodyRows.length) {
      const first = bodyRows.find(r => r.children.length === heads.length);
      if (first) {
        const w = [...first.children].map(c => c.getBoundingClientRect().width).filter(x => x > 0);
        if (w.length > 2) {
          const mx = Math.max(...w), mn = Math.min(...w);
          if (mn > 0 && mx / mn > L.columnRatio) {
            out.push({ table: tag, issue: 'LOPSIDED', detail: `widest column ${Math.round(mx)}px vs narrowest ${Math.round(mn)}px (${(mx / mn).toFixed(1)}x)` });
          }
        }
      }
    }
  });
  return out;
};

const browser = await chromium.launch();
const findings = [];
let tables = 0;
let renderFailures = 0;

for (const w of WIDTHS) {
  const ctx = await browser.newContext({ viewport: { width: w, height: 900 } });
  const page = await ctx.newPage();
  for (const path of PAGES) {
    await page.goto(BASE + path, { waitUntil: 'networkidle' });
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(250);
    if (!await assertRendered(page, path)) { renderFailures++; continue; }
    tables += await page.evaluate(() => document.querySelectorAll('table').length);
    for (const r of await page.evaluate(audit, LIMITS)) {
      findings.push({ width: w, page: path.replace('/index.html', '') || '/', ...r });
    }
  }
  await ctx.close();
}
await browser.close();

console.log(`\nTABLE FIT AUDIT  ${BASE}`);
console.log(`checked ${tables} rendered tables across ${PAGES.length} pages x ${WIDTHS.length} widths\n`);

if (!findings.length) {
  console.log('  PASS  no wrapping, uniform rows, balanced columns, legible and AA-contrast at every width');
} else {
  const by = {};
  for (const f of findings) (by[f.issue] ||= []).push(f);
  for (const [issue, list] of Object.entries(by)) {
    // collapse identical findings that repeat across widths
    const seen = new Map();
    for (const f of list) {
      const k = `${f.page}|${f.table}|${f.detail}`;
      if (!seen.has(k)) seen.set(k, { ...f, widths: [] });
      seen.get(k).widths.push(f.width);
    }
    console.log(`  ${issue}  (${seen.size} distinct)`);
    for (const f of [...seen.values()].slice(0, 8)) {
      console.log(`    ${f.page}  ${f.table}`);
      console.log(`      ${f.detail}   at ${[...new Set(f.widths)].join(', ')}px`);
    }
    if (seen.size > 8) console.log(`    ... and ${seen.size - 8} more`);
  }
}
if (renderFailures) {
  console.log(`\n  FAIL  ${renderFailures} page render(s) were empty or truncated and could not be audited`);
}
process.exit(findings.length || renderFailures ? 1 : 0);
