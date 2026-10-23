import { mkdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';

export default function globalSetup() {
  const out = process.env.SFS_VISUAL_OUT!;
  mkdirSync(out, { recursive: true });
  // Only remove what this harness writes; the directory may hold other files.
  rmSync(join(out, 'report.jsonl'), { force: true });
}
