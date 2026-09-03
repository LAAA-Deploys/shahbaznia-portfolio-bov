/**
 * Navigation audit: can a reader actually reach every section?
 *
 * The sticky nav scrolls horizontally on a phone. Two defects shipped here and
 * neither was visible to a link-checker or a screenshot:
 *
 *  1. `justify-content: center` on an overflowing flex scroller pushes the
 *     LEADING links into negative space that scrollLeft:0 cannot reach, so
 *     Portfolio and Track Record were permanently unreachable on mobile. Fixed
 *     with `justify-content: safe center`.
 *  2. `padding-right` collapses on an overflowing flex scroller, so the final
 *     link (Contact) could never be brought fully into view. Fixed with a
 *     zero-width trailing flex spacer.
 *
 * This sweeps the scroller end to end at every breakpoint and fails if any link
 * is never fully visible at any scroll position.
 *
 * Usage: node audit_nav.mjs [baseUrl]
 */
import { chromium } from 'playwright';
import { discoverPages, assertRendered } from './audit_pages.mjs';
import { probeNavigation } from './nav_probe.mjs';
const PAGES = discoverPages();   // every staged route, never a hardcoded list
const BASE = process.argv[2] || 'http://localhost:8901';
const b = await chromium.launch(); let bad=0; let renderFailures=0;
for (const w of [320,390,768,1440]) {
  const ctx = await b.newContext({ viewport:{width:w,height:844}, hasTouch:w<700, isMobile:w<700 });
  const p = await ctx.newPage();
  for (const path of PAGES) {
    await p.goto(BASE+path,{waitUntil:'networkidle'});
    // Guard before the probe: on a truncated page getElementById('toc-nav')
    // returns null and the probe died with an unguarded TypeError, which also
    // aborted the whole sweep so no later page was checked at all.
    if (!await assertRendered(p, path)) { renderFailures++; continue; }
    const r = await p.evaluate(probeNavigation);
    const tag=(path.replace('/index.html','')||'/').padEnd(12);
    if(r.error){bad++;console.log(`  ${w}px ${tag} ${r.error}`);}
    else if(r.unreachable.length){bad++;console.log(`  ${w}px ${tag} UNREACHABLE: ${r.unreachable.join(', ')}`);}
    else console.log(`  ${w}px ${tag} all ${r.total} links reachable (scrollable ${r.overflow}px)`);
  }
  await ctx.close();
}
await b.close();
if (renderFailures) console.log(`\n  FAIL  ${renderFailures} page render(s) were empty or truncated`);
console.log(bad||renderFailures ? `\n  ${bad+renderFailures} FAILURES` : '\n  PASS every nav link is reachable at every width on every page');
process.exit(bad||renderFailures ?1:0);
