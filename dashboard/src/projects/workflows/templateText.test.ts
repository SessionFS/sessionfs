import { describe, expect, it } from 'vitest';
import type { WorkflowDefinition } from '../../api/workflows';
import { NEW_TEMPLATE_TEXT, parseTemplateText, templateToYaml, TemplateTextError } from './templateText';
import { template } from './fixtures';

describe('parseTemplateText', () => {
  it('reads the documented YAML example, including block scalars and flow lists', () => {
    const t = parseTemplateText(`# comment
name: Ship a change
version: 1
steps:
  - id: dispatch
    title: Split the change
    role: dispatcher
    agent: scout
    brief_template: |
      Read {ticket.title}.

      {ticket.description}
  - id: implement
    title: "Implement: one brief"
    role: implementer
    agent: atlas
    depends_on: [dispatch]
    fan_out: 3
  - id: verify
    title: Security verification
    role: verifier
    agent: sentinel
    depends_on:
      - implement
    gate: approval_policy:security
`);
    expect(t.name).toBe('Ship a change');
    const steps = t.definition.steps as Record<string, unknown>[];
    expect(steps).toHaveLength(3);
    expect(steps[0].brief_template).toBe('Read {ticket.title}.\n\n{ticket.description}\n');
    expect(steps[1]).toMatchObject({ title: 'Implement: one brief', depends_on: ['dispatch'], fan_out: 3 });
    expect(steps[2]).toMatchObject({ depends_on: ['implement'], gate: 'approval_policy:security' });
  });

  it('accepts JSON', () => {
    const t = parseTemplateText('{"name":"X","steps":[{"id":"a","title":"A","role":"implementer","agent":"atlas"}]}');
    expect(t.name).toBe('X');
    expect((t.definition.steps as unknown[]).length).toBe(1);
  });

  it('parses the starter text', () => {
    expect(parseTemplateText(NEW_TEMPLATE_TEXT).name).toBe('Ship a change');
  });

  it.each([
    ['', /Write a template first/],
    ['steps: []', /needs a name/],
    ['{"name": ', /Not valid JSON/],
    ['name: A\nsteps:\n  - id: a\n   title: b', /Line \d+/],
    ['name: A\nname: B', /duplicate key/],
    ['name: A\nsteps: {a: 1}', /inline maps/],
    ['- a\n- b', /mapping/],
  ])('refuses %j with a readable error', (text, msg) => {
    expect(() => parseTemplateText(text)).toThrow(TemplateTextError);
    expect(() => parseTemplateText(text)).toThrow(msg);
  });
});

describe('templateToYaml', () => {
  it('round-trips a definition', () => {
    const tpl = template();
    const yaml = templateToYaml(tpl.name, tpl.definition as WorkflowDefinition);
    const back = parseTemplateText(yaml);
    expect(back.name).toBe(tpl.name);
    const steps = back.definition.steps as Record<string, unknown>[];
    for (const [i, s] of (tpl.definition as WorkflowDefinition).steps.entries()) {
      expect(steps[i]).toEqual({
        id: s.id,
        title: s.title,
        role: s.role,
        agent: s.agent,
        ...(s.depends_on.length ? { depends_on: s.depends_on } : {}),
        ...(s.fan_out !== 1 ? { fan_out: s.fan_out } : {}),
        ...(s.gate !== 'none' ? { gate: s.gate } : {}),
        ...(s.brief_template ? { brief_template: s.brief_template } : {}),
      });
    }
  });

  it('quotes values that would not survive as plain scalars', () => {
    const yaml = templateToYaml('A: b', {
      version: 1,
      steps: [{ id: 'a', title: '123', role: 'implementer', agent: 'atlas', depends_on: [], fan_out: 1, gate: 'none', brief_template: ' leading\nx' }],
    });
    const back = parseTemplateText(yaml);
    expect(back.name).toBe('A: b');
    expect((back.definition.steps as Record<string, unknown>[])[0]).toMatchObject({ title: '123', brief_template: ' leading\nx' });
  });
});

describe('parser fidelity (never reads a file differently from the CLI)', () => {
  const step = (extra: string) => `name: T\nsteps:\n  - id: a\n    title: A\n    role: implementer\n    agent: atlas\n${extra}`;

  it.each([
    ['anchor', step('    depends_on: &deps [b]\n'), /anchors/],
    ['alias', step('    gate: *g\n'), /aliases/],
    ['tag', step('    fan_out: !!int 2\n'), /tags/],
    ['tagged item', step('    depends_on:\n      - !x b\n'), /tags/],
    ['indentation indicator', step('    brief_template: |2\n      x\n'), /indentation indicators/],
    ['indicator with chomping', step('    brief_template: >-2\n      x\n'), /indentation indicators/],
    ['more-indented folded line', step('    brief_template: >\n      a\n        b\n'), /more-indented/],
    ['line indented less than the first', step('    brief_template: |\n        a\n      b\n'), /indented less/],
    ['yes', step('    title2: yes\n'), /true, false or null/],
    ['Off', step('    fan_out: Off\n'), /true, false or null/],
    ['y', step('    agent2: y\n'), /true, false or null/],
    ['True', step('    flag: True\n'), /true, false or null/],
    ['NULL', step('    gate: NULL\n'), /true, false or null/],
    ['float', step('    fan_out: 1.5\n'), /not a plain integer/],
    ['octal', step('    fan_out: 012\n'), /leading zero/],
    ['hex', step('    fan_out: 0x1f\n'), /not a plain integer/],
    ['date', step('    when: 2026-11-06\n'), /not a plain integer/],
    ['bool key', 'name: T\nyes: 1\nsteps: []\n', /cannot be a key/],
    ['unknown top-level key', 'name: T\nowner: me\nsteps:\n  - id: a\n    title: A\n    role: implementer\n    agent: atlas\n', /Unknown top-level key: owner/],
    ['keys next to definition', 'name: T\nversion: 1\ndefinition:\n  steps: []\n', /next to definition/],
  ])('refuses %s', (_label, text, msg) => {
    expect(() => parseTemplateText(text)).toThrow(TemplateTextError);
    expect(() => parseTemplateText(text)).toThrow(msg);
  });

  it('accepts the unambiguous forms and a nested definition', () => {
    const t = parseTemplateText('name: T\ndefinition:\n  version: 1\n  steps:\n    - id: a\n      title: "yes"\n      role: implementer\n      agent: atlas\n      fan_out: 2\n');
    expect((t.definition.steps as Record<string, unknown>[])[0]).toMatchObject({ title: 'yes', fan_out: 2 });
  });

  it('folds like YAML: blank lines become newlines', () => {
    const t = parseTemplateText(step('    brief_template: >\n      a\n      b\n\n      c\n\n\n      d\n'));
    expect((t.definition.steps as Record<string, unknown>[])[0].brief_template).toBe('a b\nc\n\nd\n');
  });

  it('keeps trailing newlines with |+ and drops them with |-', () => {
    const keep = parseTemplateText(step('    brief_template: |+\n      a\n\n'));
    expect((keep.definition.steps as Record<string, unknown>[])[0].brief_template).toBe('a\n\n');
    const strip = parseTemplateText(step('    brief_template: |-\n      a\n      b\n'));
    expect((strip.definition.steps as Record<string, unknown>[])[0].brief_template).toBe('a\nb');
  });

  it.each([['a\nb'], ['a\nb\n'], ['a\nb\n\n'], ['a\n\n  b\n'], ['\n'], ['x']])(
    'round-trips the brief %j exactly, including its trailing newline',
    (brief) => {
      const yaml = templateToYaml('T', {
        version: 1,
        steps: [{ id: 'a', title: 'on', role: 'implementer', agent: 'atlas', depends_on: [], fan_out: 1, gate: 'none', brief_template: brief }],
      });
      const back = parseTemplateText(yaml).definition.steps as Record<string, unknown>[];
      expect(back[0].brief_template).toBe(brief);
      expect(back[0].title).toBe('on');
    },
  );
});
