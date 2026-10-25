/** DOM id of a rendered message — stable across pages and sort order. */
export function messageAnchorId(index: number): string {
  return `msg-${index}`;
}
