// Per-sentence evidence for helper answers (the server's `grounding` map).
//
// The server resolves the model's evidence ids: an id about one ticket, wiki
// page, session or docs section is already a normal `[ticket:…]`-style marker
// in the text (so it renders as a citation chip). This module handles the
// rest before the citation pass runs:
//   - a sentence backed only by a list/search result gets a quiet chip that
//     names the lookup (`#sfs-evidence-<id>`);
//   - a sentence with no source loses its " (not verified)" text and is
//     wrapped in a link to `#sfs-unverified`, rendered as a dotted underline
//     with a tooltip.
// Offsets are UTF-16 code units, i.e. plain JavaScript string indexes.

import type { HelperGrounding, HelperGroundingEvidence } from '../api/client';

export const UNVERIFIED_HREF = '#sfs-unverified';
const EVIDENCE_PREFIX = '#sfs-evidence-';

export function evidenceHref(id: string): string {
  return `${EVIDENCE_PREFIX}${id}`;
}

/** `#sfs-evidence-e12` → `e12`, else null. */
export function parseEvidenceHref(href: string): string | null {
  if (!href.startsWith(EVIDENCE_PREFIX)) return null;
  const id = href.slice(EVIDENCE_PREFIX.length);
  return /^e\d{1,6}$/.test(id) ? id : null;
}

/** Escape text so it can sit inside a markdown link label. */
function escapeLabel(text: string): string {
  return text.replace(/([\\[\]])/g, '\\$1');
}

const MARKER_IN_TEXT = /\[(?:session|ticket|wiki|doc|meeting|chat):[^\]\n]+\]/;

interface Edit {
  start: number;
  end: number;
  replacement: string;
}

/**
 * Rewrite the answer text with the grounding map applied (see the module
 * comment). Returns the text unchanged when the map doesn't fit it (a
 * sentence range outside the text), so a stale map never garbles an answer.
 */
export function applyGrounding(text: string, grounding: HelperGrounding | null | undefined): string {
  if (!grounding || grounding.mode !== 'strict' || !grounding.sentences?.length) return text;
  const evidence = new Map<string, HelperGroundingEvidence>();
  for (const e of grounding.evidence ?? []) evidence.set(e.id, e);

  const edits: Edit[] = [];
  for (const s of grounding.sentences) {
    if (!(s.start >= 0 && s.end <= text.length && s.start < s.end)) return text;
    const body = text.slice(s.start, s.end);
    if (s.status === 'unverified') {
      let content = body;
      const flag = s.flag;
      if (flag && flag[0] >= s.start && flag[1] <= s.end && flag[0] < flag[1]) {
        content = text.slice(s.start, flag[0]) + text.slice(flag[1], s.end);
      }
      // Keep a trailing terminator outside the link so it reads naturally.
      const m = /([.!?]["')\]”’]*)$/.exec(content);
      const inner = m ? content.slice(0, -m[1].length) : content;
      const tail = m ? m[1] : '';
      if (!inner.trim()) continue;
      edits.push({ start: s.start, end: s.end, replacement: `[${escapeLabel(inner)}](${UNVERIFIED_HREF})${tail}` });
    } else if (s.status === 'supported' && !MARKER_IN_TEXT.test(body)) {
      const chips = (s.evidence ?? [])
        .filter((id) => evidence.has(id) && !evidence.get(id)!.marker)
        .map((id) => ` [${id}](${evidenceHref(id)})`)
        .join('');
      if (chips) edits.push({ start: s.end, end: s.end, replacement: chips });
    }
  }
  let out = text;
  for (const e of edits.sort((a, b) => b.start - a.start || b.end - a.end)) {
    out = out.slice(0, e.start) + e.replacement + out.slice(e.end);
  }
  return out;
}
