/**
 * Browser-side navigation probe, kept importable for a no-browser regression.
 */
export async function probeNavigation(doc = document) {
  const n = doc.getElementById('toc-nav');
  if (!n) {
    return {
      total: 0,
      unreachable: [],
      overflow: 0,
      error: 'missing #toc-nav',
    };
  }

  n.style.scrollBehavior = 'auto';
  const links = [...n.querySelectorAll('a')];
  const unreachable = [];
  const seen = new Set();
  const max = n.scrollWidth - n.clientWidth;
  for (let x = 0; x <= max + 1; x += 20) {
    n.scrollLeft = x;
    await new Promise(resolve => requestAnimationFrame(resolve));
    const nr = n.getBoundingClientRect();
    links.forEach(a => {
      const ar = a.getBoundingClientRect();
      if (ar.left >= nr.left - 2 && ar.right <= nr.right + 2) {
        seen.add(a.textContent.trim());
      }
    });
  }
  links.forEach(a => {
    const text = a.textContent.trim();
    if (!seen.has(text)) unreachable.push(text);
  });
  return { total: links.length, unreachable, overflow: max, error: null };
}
