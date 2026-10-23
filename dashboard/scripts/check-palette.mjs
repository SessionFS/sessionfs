#!/usr/bin/env node
/**
 * Guard against hard-coded colours in dashboard source.
 *
 * All colour comes from the design tokens in src/index.css (@theme), so a
 * theme change reaches every surface. This fails when a .ts/.tsx file under
 * src/ (tests excluded) contains:
 *   - rgb()/rgba()/hsl()/hsla()/oklch() literals
 *   - hex colour literals (#abc, #aabbcc, #aabbccdd)
 *   - Tailwind default-palette utilities (text-red-500, bg-blue-600, ...)
 *
 * Plain Node, no dependencies. Run: npm run check:palette
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');

/**
 * Explicit exceptions. Each entry is a file (relative to dashboard/, posix
 * separators) plus the rule it is exempt from, with the reason it is data and
 * not styling. Keep this list short.
 */
const ALLOWLIST = [
  {
    file: 'src/utils/folderColors.ts',
    rule: 'hex',
    reason: 'Swatch choices for user-created folders. The chosen hex is stored per folder on the server, so these are data, not theme styling.',
  },
];

const PALETTE =
  'slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose';
const UTILITY =
  'text|bg|border|ring|ring-offset|fill|stroke|from|via|to|divide|outline|decoration|shadow|accent|caret|placeholder';

const RULES = [
  {
    id: 'color-function',
    re: /\b(?:rgba?|hsla?|oklch|oklab|lab|lch)\(/g,
    hint: 'use a semantic token (var(--...) or a themed utility)',
  },
  {
    id: 'hex',
    // #rgb, #rgba, #rrggbb, #rrggbbaa not glued to a word/number on either side
    re: /(?<![\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])/g,
    hint: 'use a semantic token instead of a hex colour',
  },
  {
    id: 'tailwind-palette',
    re: new RegExp(`(?<![\\w-])(?:${UTILITY})-(?:${PALETTE})-(?:50|[1-9]00|950)(?![\\w-])`, 'g'),
    hint: 'use a semantic utility (text-danger, bg-brand, border-border, ...)',
  },
];

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) {
      if (name === '__tests__' || name === 'node_modules') continue;
      walk(p, out);
    } else if (/\.(ts|tsx)$/.test(name) && !/\.(test|spec)\.(ts|tsx)$/.test(name) && !name.endsWith('.d.ts')) {
      out.push(p);
    }
  }
  return out;
}

/** Blank out comments (keeping line numbers) so prose like "issue #123" is not flagged. */
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:\\'"`])\/\/[^\n]*/g, (m, pre) => pre + ' '.repeat(m.length - pre.length));
}

const allowed = (file, rule) => ALLOWLIST.some((a) => a.file === file && a.rule === rule);

const hits = [];
for (const abs of walk(SRC)) {
  const file = relative(ROOT, abs).split(sep).join('/');
  const lines = stripComments(readFileSync(abs, 'utf8')).split('\n');
  lines.forEach((line, i) => {
    for (const rule of RULES) {
      if (allowed(file, rule.id)) continue;
      for (const m of line.matchAll(rule.re)) {
        hits.push({ file, line: i + 1, rule: rule.id, text: m[0], hint: rule.hint });
      }
    }
  });
}

if (hits.length === 0) {
  console.log('palette check: no hard-coded colours found');
  process.exit(0);
}
console.error(`palette check: ${hits.length} hard-coded colour(s)\n`);
for (const h of hits) console.error(`  ${h.file}:${h.line}  [${h.rule}] ${h.text}  -> ${h.hint}`);
console.error('\nColours belong in src/index.css tokens. If a value is genuinely data (e.g. a third-party');
console.error('brand colour), add an ALLOWLIST entry in scripts/check-palette.mjs with the reason.');
process.exit(1);
