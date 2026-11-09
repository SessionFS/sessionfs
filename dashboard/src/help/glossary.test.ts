/// <reference types="node" />
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { GLOSSARY as GLOSSARY_LITERAL, docsUrl, getGlossaryEntry, type GlossaryEntry } from './glossary';

const GLOSSARY: readonly GlossaryEntry[] = GLOSSARY_LITERAL;

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, '..');
const DOCS = join(HERE, '..', '..', '..', 'site', 'src', 'content', 'docs');

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...sourceFiles(p));
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) out.push(p);
  }
  return out;
}

describe('glossary', () => {
  it('has unique, kebab-case ids', () => {
    const ids = GLOSSARY.map((e) => e.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const id of ids) expect(id).toMatch(/^[a-z0-9]+(-[a-z0-9]+)*$/);
  });

  it('keeps every definition short and plain', () => {
    for (const e of GLOSSARY) {
      expect(e.term.trim(), e.id).not.toBe('');
      expect(e.definition.length, e.id).toBeLessThanOrEqual(220);
      expect(e.definition, e.id).toMatch(/\.$/);
      // House style: no hype, no filler, no emoji.
      expect(e.definition, e.id).not.toMatch(/!|\b(simply|just|easily|seamless(ly)?|powerful)\b/i);
      expect(e.definition, e.id).not.toMatch(/\p{Extended_Pictographic}/u);
      // At most two sentences.
      const sentences = e.definition.split(/\.(?:\s|$)/).filter((s) => s.trim().length > 0);
      expect(sentences.length, e.id).toBeLessThanOrEqual(2);
      // Don't open by restating the label.
      expect(e.definition.toLowerCase().startsWith(`${e.term.toLowerCase()} is`), e.id).toBe(false);
    }
  });

  it('links only to docs pages that exist', () => {
    for (const e of GLOSSARY) {
      if (!e.docs) continue;
      const found = ['.mdx', '.md'].some((ext) => existsSync(join(DOCS, `${e.docs}${ext}`)));
      expect(found, `${e.id} -> ${e.docs}`).toBe(true);
      expect(docsUrl(e.docs)).toBe(`https://sessionfs.dev/${e.docs}/`);
    }
  });

  it('resolves every <Term id> used in the dashboard', () => {
    const used = new Set<string>();
    for (const file of sourceFiles(SRC)) {
      const text = readFileSync(file, 'utf8');
      for (const m of text.matchAll(/<(?:Term|TermTip)\s+id="([a-z0-9-]+)"/g)) used.add(m[1]);
    }
    expect(used.size).toBeGreaterThan(0);
    const missing = [...used].filter((id) => !getGlossaryEntry(id));
    expect(missing).toEqual([]);
  });
});
