import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

interface Row {
  page: string;
  theme: string;
  width: number;
  file: string;
  overflowPx: number;
  broken: string | null;
  clipped?: string[];
  errors: string[];
  axe: { id: string; impact: string | null; nodes: number; targets: string[]; detail?: string[] }[];
}

/** Aggregate the per-screenshot rows into report.json and print a summary. */
export default function globalTeardown() {
  const out = process.env.SFS_VISUAL_OUT!;
  const src = join(out, 'report.jsonl');
  if (!existsSync(src)) return;
  const rows: Row[] = readFileSync(src, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((l) => JSON.parse(l));
  rows.sort((a, b) => a.file.localeCompare(b.file));
  writeFileSync(join(out, 'report.json'), JSON.stringify(rows, null, 2));

  const overflow = rows.filter((r) => r.overflowPx > 0);
  const broken = rows.filter((r) => r.broken);
  const clipped = rows.filter((r) => r.clipped && r.clipped.length > 0);
  console.log(`\nvisual harness: ${rows.length} screenshots in ${out}`);
  console.log(`horizontal overflow: ${overflow.length}`);
  for (const r of overflow) console.log(`  ${r.page} ${r.theme} ${r.width}px: +${r.overflowPx}px`);
  console.log(`clipped (past viewport edge): ${clipped.length}`);
  for (const r of clipped) console.log(`  ${r.page} ${r.theme} ${r.width}px: ${r.clipped!.slice(0, 3).join(' | ')}`);
  console.log(`broken renders: ${broken.length}`);
  for (const r of broken) console.log(`  ${r.page} ${r.theme} ${r.width}px: ${r.broken}`);
  if (clipped.length > 0) throw new Error(`${clipped.length} page(s) have content past the viewport edge`);
}
