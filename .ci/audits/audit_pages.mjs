// Shared route discovery and the empty-output guard for all three audits.
//
// Two defects this exists to kill, both proven by falsification on 2026-07-30:
//
// 1. HARDCODED PAGE LISTS. All three audits carried an identical
//    PAGES = ['/index.html', '/property-one/index.html', '/property-two/index.html'].
//    A fourth route with no webfonts, a 1400px container, an inlined base64
//    image, a wrapping table cell, a 404 image and a stretched image shipped
//    past every one of them with a clean PASS, and the "checked N across 3
//    pages" counters did not move. This is a generator whose entire purpose is
//    to add deal pages, so a hardcoded list of the pages that existed on the day
//    it was written is the worst possible default.
//
// 2. SILENT PASS ON EMPTY OUTPUT. Truncating a page to an 81-byte shell made
//    both the table and media audits report PASS. A template render that
//    silently produces nothing is close to the worst failure available, and it
//    was invisible, because "zero tables found" and "zero broken tables" are the
//    same result to a checker that only counts defects.
//
// Discovery reads the STAGED artifact, so the audits check what actually
// deploys rather than what happens to be lying around the repo.
import { existsSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = process.env.AUDIT_ROOT || '_site';

export function discoverPages(root = ROOT) {
  if (!existsSync(root)) {
    throw new Error(`audit root "${root}" does not exist. Run the staging step first, `
      + `or set AUDIT_ROOT to the directory being served.`);
  }
  const pages = [];
  if (existsSync(join(root, 'index.html'))) pages.push('/index.html');
  for (const name of readdirSync(root)) {
    const dir = join(root, name);
    if (!statSync(dir).isDirectory()) continue;
    if (existsSync(join(dir, 'index.html'))) pages.push(`/${name}/index.html`);
  }
  if (!pages.length) throw new Error(`no route files found under "${root}"`);
  return pages;
}

// Floors, not targets. Any real page clears these by a wide margin; only a
// broken or empty render fails them. Set from the SMALLEST legitimate page,
// which is the portfolio hub: 4 sections and roughly 7,000 characters. A floor
// tuned to the property pages would fail the hub, and a gate that fails on
// correct output gets switched off.
const MIN_SECTIONS = 3;
const MIN_TEXT = 2000;

export async function assertRendered(page, path) {
  const shape = await page.evaluate(() => ({
    sections: document.querySelectorAll('.section').length,
    nav: document.querySelectorAll('.toc-nav a').length,
    text: (document.body.innerText || '').trim().length,
  }));
  const bad = [];
  if (shape.sections < MIN_SECTIONS) bad.push(`${shape.sections} sections (need ${MIN_SECTIONS})`);
  if (!shape.nav) bad.push('no navigation links');
  if (shape.text < MIN_TEXT) bad.push(`${shape.text} chars of text (need ${MIN_TEXT})`);
  if (bad.length) {
    console.log(`\n  FAIL  ${path} did not render: ${bad.join(', ')}`);
    console.log('        An empty or truncated page passes every defect-counting');
    console.log('        check, because zero elements means zero broken elements.');
    return false;
  }
  return true;
}
