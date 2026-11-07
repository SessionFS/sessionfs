import type { WorkflowDefinition } from '../../api/workflows';

/**
 * The text form of a template: the same file the CLI reads
 * (`sfs workflow create file.yaml`), as YAML or JSON.
 *
 * There is no YAML library in the dashboard on purpose. This reads the small
 * subset a template uses: mappings, lists (`- item` and `[a, b]`), plain and
 * quoted scalars, and `|` / `>` block scalars. Anything else is a readable
 * error with a line number; the server then validates the result.
 *
 * Anything a full YAML reader (the CLI's) could read differently is refused
 * rather than guessed: anchors, aliases and tags; block indentation
 * indicators; more-indented lines in folded blocks; and plain scalars YAML
 * 1.1 resolves to something other than a string, integer, true/false or
 * null (yes/no/on/off/y/n, True/FALSE, Null, 1.5, 0x1f, 012, dates...).
 * The CLI refuses the same scalars, so both read a file the same way.
 */

export class TemplateTextError extends Error {}

interface Line {
  indent: number;
  text: string;
  no: number;
}
type Value = string | number | boolean | null | Value[] | { [k: string]: Value };

const fail = (no: number, msg: string): never => {
  throw new TemplateTextError(`Line ${no}: ${msg}`);
};

function splitFlow(inner: string, no: number): string[] {
  const out: string[] = [];
  let cur = '';
  let quote: string | null = null;
  for (const ch of inner) {
    if (quote) {
      cur += ch;
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      cur += ch;
    } else if (ch === ',') {
      out.push(cur);
      cur = '';
    } else if (ch === '[' || ch === '{') {
      fail(no, 'nested lists and inline maps are not supported; use block style');
    } else {
      cur += ch;
    }
  }
  if (quote) fail(no, 'unterminated quote');
  if (cur.trim() !== '' || out.length > 0) out.push(cur);
  return out;
}

function stripComment(s: string): string {
  let quote: string | null = null;
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (quote) {
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") {
      if (i === 0 || /[\s[,]/.test(s[i - 1])) quote = ch;
    } else if (ch === '#' && (i === 0 || /\s/.test(s[i - 1]))) {
      return s.slice(0, i);
    }
  }
  return s;
}

const BOOLISH = new Set(['yes', 'no', 'on', 'off', 'y', 'n', 'true', 'false', 'null']);
const NUMBERISH_RE = /^[-+]?(\.\d|\d)[\d_.:eE+-]*$|^[-+]?\.(inf|nan)$|^[-+]?0[xob][0-9a-f_]+$/i;

/** Why a plain scalar is ambiguous between YAML readers, or null when it is not. */
export function ambiguousPlain(s: string): string | null {
  const lower = s.toLowerCase();
  if (BOOLISH.has(lower) && s !== 'true' && s !== 'false' && s !== 'null') {
    return `"${s}" reads differently in YAML readers; quote it, or write true, false or null`;
  }
  if (/^-?\d+$/.test(s)) {
    return /^-?0\d/.test(s) ? `"${s}" has a leading zero (an octal number in YAML); quote it or drop the zero` : null;
  }
  if (NUMBERISH_RE.test(s) || /^\d{4}-\d{1,2}-\d{1,2}/.test(s) || s === '=') {
    return `"${s}" is not a plain integer, so YAML readers may not read it as text; quote it`;
  }
  return null;
}

function scalar(raw: string, no: number): Value {
  const s = stripComment(raw).trim();
  if (s === '' || s === '~' || s === 'null') return null;
  if (/^[&*!]/.test(s)) return fail(no, 'anchors (&), aliases (*) and tags (!) are not supported');
  if (/^[|>]/.test(s)) return fail(no, 'block scalars with indentation indicators are not supported; use | or >');
  if (/^[%@`]/.test(s)) return fail(no, `"${s[0]}" cannot start a plain value; quote it`);
  if (s.startsWith('"')) {
    try {
      return JSON.parse(s) as string;
    } catch {
      return fail(no, 'invalid double-quoted string');
    }
  }
  if (s.startsWith("'")) {
    if (!s.endsWith("'") || s.length < 2) return fail(no, 'unterminated quote');
    return s.slice(1, -1).replace(/''/g, "'");
  }
  if (s.startsWith('[')) {
    if (!s.endsWith(']')) return fail(no, 'unterminated list');
    return splitFlow(s.slice(1, -1), no).map((p) => scalar(p, no));
  }
  if (s.startsWith('{')) return fail(no, 'inline maps are not supported; use block style');
  const why = ambiguousPlain(s);
  if (why) return fail(no, why);
  if (s === 'true') return true;
  if (s === 'false') return false;
  if (/^-?\d+$/.test(s)) return Number(s);
  return s;
}

const KEY_RE = /^([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?$/;

export function parseYamlSubset(text: string): Value {
  const raw = text.replace(/\r\n?/g, '\n').split('\n');
  // The newline that ends the last line does not start another line.
  if (raw.length > 1 && raw[raw.length - 1] === '') raw.pop();
  if (raw.some((l) => l.includes('\t') && /^\s*\t/.test(l))) {
    throw new TemplateTextError('Tabs cannot indent YAML; use spaces.');
  }
  const lines: Line[] = raw.map((l, i) => ({
    indent: l.length - l.trimStart().length,
    text: l.trim(),
    no: i + 1,
  }));
  let pos = 0;
  const skipBlank = () => {
    while (pos < lines.length && (lines[pos].text === '' || lines[pos].text.startsWith('#'))) pos++;
  };

  function blockScalar(parentIndent: number, style: string): string {
    const folded = style.startsWith('>');
    const keep = style.includes('+');
    const strip = style.includes('-');
    const body: string[] = [];
    let base = -1;
    while (pos < lines.length) {
      const l = lines[pos];
      if (l.text === '') {
        if (base >= 0 && raw[pos].length > base) {
          fail(l.no, 'a blank line inside a block has trailing spaces; remove them');
        }
        body.push('');
        pos++;
        continue;
      }
      if (l.indent <= parentIndent) break;
      if (base < 0) base = l.indent;
      if (l.indent < base) fail(l.no, 'this block line is indented less than the first line of the block');
      if (folded && l.indent > base) {
        fail(l.no, 'more-indented lines inside a folded (>) block are not supported; use | instead');
      }
      body.push(raw[pos].slice(base));
      pos++;
    }
    let last = body.length;
    while (last > 0 && body[last - 1] === '') last--;
    const trailing = body.length - last;
    const content = body.slice(0, last);
    let out: string;
    if (folded) {
      // YAML folding: a single break between text lines becomes a space;
      // each blank line in a run becomes one newline.
      let lead = 0;
      while (lead < content.length && content[lead] === '') lead++;
      out = '\n'.repeat(lead);
      for (let i = lead; i < content.length; i++) {
        const line = content[i];
        if (i === lead) out += line;
        else if (line === '') out += '\n';
        else out += content[i - 1] === '' ? line : ` ${line}`;
      }
    } else {
      out = content.join('\n');
    }
    if (content.length > 0 && !strip) out += '\n';
    if (keep) out += '\n'.repeat(trailing);
    return out;
  }

  function parseValue(rest: string, indent: number, no: number): Value {
    const r = stripComment(rest).trim();
    if (/^[|>][+-]?$/.test(r)) return blockScalar(indent, r);
    if (/^[|>]/.test(r)) fail(no, 'block scalars with indentation indicators (like |2) are not supported; use | or >');
    if (r === '') {
      skipBlank();
      if (pos < lines.length) {
        const nxt = lines[pos];
        if (nxt.indent > indent || (nxt.indent === indent && nxt.text.startsWith('-'))) return parseBlock(nxt.indent);
      }
      return null;
    }
    return scalar(rest, no);
  }

  function parseBlock(indent: number): Value {
    skipBlank();
    if (pos >= lines.length) return null;
    return lines[pos].text.startsWith('-') ? parseSeq(indent) : parseMap(indent);
  }

  function parseSeq(indent: number): Value[] {
    const out: Value[] = [];
    for (;;) {
      skipBlank();
      if (pos >= lines.length) break;
      const l = lines[pos];
      if (l.indent !== indent || !(l.text === '-' || l.text.startsWith('- '))) {
        if (l.indent > indent) fail(l.no, 'unexpected indentation');
        break;
      }
      const rest = l.text === '-' ? '' : l.text.slice(2);
      const lead = rest.length - rest.trimStart().length;
      const item = rest.trimStart();
      if (item !== '' && KEY_RE.test(stripComment(item).trim()) && !item.startsWith('"') && !item.startsWith("'")) {
        // "- key: value": the mapping continues on the lines below at this column.
        lines[pos] = { indent: indent + 2 + lead, text: item, no: l.no };
        out.push(parseMap(indent + 2 + lead));
      } else {
        pos++;
        out.push(parseValue(item, indent, l.no));
      }
    }
    return out;
  }

  function parseMap(indent: number): { [k: string]: Value } {
    const out: { [k: string]: Value } = {};
    for (;;) {
      skipBlank();
      if (pos >= lines.length) break;
      const l = lines[pos];
      if (l.indent < indent) break;
      if (l.indent > indent) fail(l.no, 'unexpected indentation');
      const m = KEY_RE.exec(stripComment(l.text).trimEnd());
      if (!m) fail(l.no, `expected "key: value", found "${l.text}"`);
      const key = m![1];
      if (BOOLISH.has(key.toLowerCase())) fail(l.no, `"${key}" cannot be a key: YAML readers treat it as a true/false or null value`);
      if (key in out) fail(l.no, `duplicate key "${key}"`);
      // Keep the original (comment-bearing) remainder so block-scalar markers parse.
      const restOfLine = l.text.slice(key.length + 1);
      pos++;
      out[key] = parseValue(restOfLine, indent, l.no);
    }
    return out;
  }

  skipBlank();
  if (pos >= lines.length) throw new TemplateTextError('The template is empty.');
  const first = lines[pos];
  const value = parseBlock(first.indent);
  skipBlank();
  if (pos < lines.length) fail(lines[pos].no, 'unexpected content');
  return value;
}

export interface ParsedTemplate {
  name: string;
  definition: Record<string, unknown>;
}

/** Turn editor text (YAML or JSON) into the request body, or throw a readable error. */
export function parseTemplateText(text: string): ParsedTemplate {
  const trimmed = text.trim();
  if (trimmed === '') throw new TemplateTextError('Write a template first.');
  let doc: Value;
  if (trimmed.startsWith('{')) {
    try {
      doc = JSON.parse(trimmed) as Value;
    } catch (e) {
      throw new TemplateTextError(`Not valid JSON: ${e instanceof Error ? e.message : 'parse error'}`);
    }
  } else {
    doc = parseYamlSubset(text);
  }
  if (!doc || typeof doc !== 'object' || Array.isArray(doc)) {
    throw new TemplateTextError('A template is a mapping with a name and steps.');
  }
  const { name, ...rest } = doc as { [k: string]: Value };
  if (typeof name !== 'string' || name.trim() === '') {
    throw new TemplateTextError('The template needs a name: at the top.');
  }
  // Same top-level shape as `sfs workflow create`: name plus the definition
  // keys, or name plus one `definition` mapping. Nothing else.
  let definition: { [k: string]: Value };
  if ('definition' in rest) {
    const others = Object.keys(rest).filter((k) => k !== 'definition');
    if (others.length) throw new TemplateTextError(`Unexpected keys next to definition: ${others.sort().join(', ')}`);
    const inner = rest.definition;
    if (!inner || typeof inner !== 'object' || Array.isArray(inner)) {
      throw new TemplateTextError('definition must be a mapping.');
    }
    definition = inner as { [k: string]: Value };
  } else {
    definition = rest;
  }
  const unknown = Object.keys(definition).filter((k) => k !== 'version' && k !== 'steps');
  if (unknown.length) {
    throw new TemplateTextError(`Unknown top-level ${unknown.length === 1 ? 'key' : 'keys'}: ${unknown.sort().join(', ')} (a template has name, version and steps).`);
  }
  return { name: name.trim(), definition };
}

const PLAIN_RE = /^[A-Za-z0-9][A-Za-z0-9 _./():@-]*$/;

function emitScalar(s: string): string {
  if (
    PLAIN_RE.test(s) &&
    !s.endsWith(' ') &&
    !BOOLISH.has(s.toLowerCase()) &&
    !/^-?\d+$/.test(s) &&
    ambiguousPlain(s) === null &&
    !s.includes(': ') &&
    !s.includes(' #')
  ) {
    return s;
  }
  return JSON.stringify(s);
}

/** The YAML a template is edited as. Round-trips through `parseTemplateText`. */
export function templateToYaml(name: string, def: WorkflowDefinition): string {
  const out: string[] = [`name: ${emitScalar(name)}`, `version: ${def.version}`, 'steps:'];
  for (const s of def.steps) {
    out.push(`  - id: ${emitScalar(s.id)}`);
    out.push(`    title: ${emitScalar(s.title)}`);
    out.push(`    role: ${s.role}`);
    out.push(`    agent: ${emitScalar(s.agent)}`);
    if (s.depends_on.length) out.push(`    depends_on: [${s.depends_on.map(emitScalar).join(', ')}]`);
    if (s.fan_out !== 1) out.push(`    fan_out: ${s.fan_out}`);
    if (s.gate && s.gate !== 'none') out.push(`    gate: ${emitScalar(s.gate)}`);
    const brief = s.brief_template ?? '';
    if (brief !== '') {
      const body = brief.replace(/\n+$/, '');
      const finalBreaks = brief.length - body.length;
      const lines = body.split('\n');
      const blockSafe =
        finalBreaks <= 1 &&
        body !== '' &&
        lines.every((l) => !/\s$/.test(l)) &&
        !/^\s/.test(lines[0]) &&
        !brief.includes('\r');
      if (blockSafe && lines.length > 1) {
        // Chomping keeps the original ending: | ends with one newline, |- with none.
        out.push(`    brief_template: ${finalBreaks === 1 ? '|' : '|-'}`);
        for (const l of lines) out.push(l === '' ? '' : `      ${l}`);
      } else {
        out.push(`    brief_template: ${JSON.stringify(brief)}`);
      }
    }
  }
  return out.join('\n') + '\n';
}

export const NEW_TEMPLATE_TEXT = `name: Ship a change
version: 1
steps:
  - id: implement
    title: Implement the change
    role: implementer
    agent: atlas
    brief_template: |
      Implement {ticket.title}.

      {checklist}

  - id: review
    title: Independent review
    role: reviewer
    agent: codex-reviewer
    depends_on: [implement]
    gate: trusted_clean
`;
