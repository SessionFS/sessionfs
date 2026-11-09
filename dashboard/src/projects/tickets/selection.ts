/* ── Row selection ──
 *
 * Pure helpers behind the list's checkboxes: toggle one row, shift-click a
 * range (from the last row clicked to this one, in the order shown), and
 * drop rows that are no longer on screen so a bulk action never reaches a
 * ticket the filters hide. */

export function toggleId(sel: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(sel);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/**
 * Shift-click: select every row from `anchor` to `target` in `order`
 * (either direction), keeping what was already selected. Without an anchor
 * on screen it selects just the target.
 */
export function selectRange(
  order: readonly string[],
  anchor: string | null,
  target: string,
  sel: ReadonlySet<string>,
): Set<string> {
  const next = new Set(sel);
  const to = order.indexOf(target);
  if (to < 0) return next;
  const from = anchor ? order.indexOf(anchor) : -1;
  if (from < 0) {
    next.add(target);
    return next;
  }
  const [a, b] = from <= to ? [from, to] : [to, from];
  for (let i = a; i <= b; i++) next.add(order[i]);
  return next;
}

/** The selection limited to rows on screen, in the order shown. */
export function visibleSelection(sel: ReadonlySet<string>, order: readonly string[]): string[] {
  if (!sel.size) return [];
  return order.filter((id) => sel.has(id));
}
