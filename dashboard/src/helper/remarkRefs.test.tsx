import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Markdown } from '../components/ui';
import { remarkRefs, type RepoLinks } from './format';

const TK = 'tk_008676e806e94bc5';
const TK2 = 'tk_5c124c496ea14ecc';
const PJ = 'proj_a40ce92c16e9415d';
const SES = 'ses_1a2b3c4d5e6f7a8b';
const repo: RepoLinks = { provider: 'github', path: 'acme/widgets' };

/** Render through the real Markdown pipeline; return each link's text + href. */
function linksOf(md: string, opts: { repo?: RepoLinks | null; streaming?: boolean } = {}) {
  const { container, unmount } = render(<Markdown remarkPlugins={[remarkRefs(opts)]}>{md}</Markdown>);
  const links = [...container.querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href')]);
  const nested = container.querySelectorAll('a a').length;
  unmount();
  return { links, nested };
}

const ref = (kind: string, id: string) => [id, `#sfs-ref-${kind}-${id}`];

describe('remarkRefs', () => {
  it('links ticket, project and session ids in prose', () => {
    expect(linksOf(`See ${TK}, project ${PJ} and ${SES}.`).links).toEqual([
      ref('ticket', TK),
      ref('project', PJ),
      ref('session', SES),
    ]);
  });

  it('links inline code holding exactly one id, keeping it code', () => {
    const { container } = render(<Markdown remarkPlugins={[remarkRefs()]}>{`Ticket \`${TK}\` and \`sfs ticket show ${TK2}\``}</Markdown>);
    const links = container.querySelectorAll('a');
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute('href')).toBe(`#sfs-ref-ticket-${TK}`);
    expect(links[0].querySelector('code')?.textContent).toBe(TK);
  });

  it('never links inside code blocks, wherever they sit', () => {
    const cases = [
      `\`\`\`bash\nsfs ticket show ${TK}\n\`\`\``,
      `~~~\n${TK}\n~~~`,
      `Text\n\n    ${TK}`,
      `> \`\`\`\n> ${TK}\n> \`\`\``,
      `- item\n\n  \`\`\`\n  ${TK}\n  \`\`\``,
      `1. step\n\n       ${TK}`,
      `- item\n    - nested\n\n      \`\`\`\n      ${TK}\n      \`\`\``,
    ];
    for (const md of cases) expect(linksOf(md).links).toEqual([]);
  });

  it('treats a code span split across lines as one span', () => {
    expect(linksOf(`Run \`sfs ticket\nshow ${TK}\` now`).links).toEqual([]);
  });

  it('leaves existing links of every form, autolinks, URLs and paths alone', () => {
    const cases: Array<[string, Array<Array<string | null>>]> = [
      [`[the ticket ${TK}](/projects/${PJ}?ticket=${TK})`, [[`the ticket ${TK}`, `/projects/${PJ}?ticket=${TK}`]]],
      [`<https://example.com/${TK}>`, [[`https://example.com/${TK}`, `https://example.com/${TK}`]]],
      [`https://app.example.com/t/${TK}`, [[`https://app.example.com/t/${TK}`, `https://app.example.com/t/${TK}`]]],
      [`src/${TK}/notes.md`, []],
      [`![${TK}](/img.png)`, []],
    ];
    for (const [md, expected] of cases) {
      const r = linksOf(md);
      expect(r.links).toEqual(expected);
      expect(r.nested).toBe(0);
    }
  });

  it('does not nest links inside reference-style links', () => {
    for (const md of [
      `[ticket ${TK}][1]\n\n[1]: https://example.com/${TK}`,
      `[${TK}][]\n\n[${TK}]: https://example.com/x`,
      `[${TK}]\n\n[${TK}]: https://example.com/x`,
    ]) {
      const r = linksOf(md);
      expect(r.nested).toBe(0);
      expect(r.links).toHaveLength(1);
      expect(r.links[0][1]).toMatch(/^https:\/\/example\.com\//);
    }
  });

  it('ignores near-miss ids', () => {
    for (const c of ['tk_008676e806e94bc', 'tk_008676e806e94bc5f', 'xtk_008676e806e94bc5', 'TK_008676E806E94BC5', 'ses_short']) {
      expect(linksOf(c).links).toEqual([]);
    }
  });

  it('holds back a trailing reference while streaming', () => {
    expect(linksOf(`Open ${TK}`, { streaming: true }).links).toEqual([]);
    expect(linksOf(`Open ${TK} next`, { streaming: true }).links).toEqual([ref('ticket', TK)]);
    expect(linksOf(`Open ${TK}`).links).toEqual([ref('ticket', TK)]);
    expect(linksOf('Fixed in 839c3a7', { streaming: true, repo }).links).toEqual([]);
  });

  it('links PRs and commit SHAs only when a GitHub repo is known', () => {
    const text = 'Merged PR #394 at 839c3a7.';
    expect(linksOf(text).links).toEqual([]);
    expect(linksOf(text, { repo }).links).toEqual([
      ['PR #394', 'https://github.com/acme/widgets/pull/394'],
      ['839c3a7', 'https://github.com/acme/widgets/commit/839c3a7'],
    ]);
    for (const c of ['1234567', 'deadbeef', 'facade', '#a1b2c3d', '8f3a2b1c-9d4e-4f5a-b6c7-d8e9f0a1b2c3']) {
      expect(linksOf(c, { repo }).links).toEqual([]);
    }
  });

  it('links inside emphasis, lists and tables', () => {
    expect(linksOf(`**${TK}**\n\n- ${PJ}\n\n| t |\n|---|\n| ${SES} |`).links).toEqual([
      ref('ticket', TK),
      ref('project', PJ),
      ref('session', SES),
    ]);
  });
});
