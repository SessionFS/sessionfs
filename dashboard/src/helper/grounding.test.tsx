import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import type { HelperGrounding } from '../api/client';
import { axe } from '../test/axe';
import { AssistantMessage } from './HelperMessage';
import { applyGrounding, parseEvidenceHref } from './grounding';
import type { Turn } from './format';

vi.mock('../hooks/useProjects', () => ({ useProject: () => ({ data: undefined }) }));

const actions = { onNewConversation: () => {}, onNavigate: () => {} };

const TK = 'tk_0123456789abcdef';
const SUPPORTED = `Ticket B is done [ticket:${TK}].`;
const LISTED = 'There are 3 open tickets.';
const FLAGGED = 'The runs are still dry runs (not verified).';
const TEXT = `${SUPPORTED} ${LISTED} ${FLAGGED}`;

function grounding(): HelperGrounding {
  const s2 = SUPPORTED.length + 1;
  const s3 = s2 + LISTED.length + 1;
  const flagStart = s3 + 'The runs are still dry runs'.length;
  return {
    mode: 'strict',
    unverified_count: 1,
    sentences: [
      { start: 0, end: SUPPORTED.length, status: 'supported', evidence: ['e1'] },
      { start: s2, end: s2 + LISTED.length, status: 'supported', evidence: ['e2'] },
      {
        start: s3,
        end: s3 + FLAGGED.length,
        status: 'unverified',
        evidence: [],
        flag: [flagStart, flagStart + ' (not verified)'.length],
      },
    ],
    evidence: [
      { id: 'e1', tool: 'get_ticket', args: { ticket_id: TK }, at: null, marker: `[ticket:${TK}]` },
      { id: 'e2', tool: 'list_tickets', args: {}, at: '2026-11-08T10:00:00+00:00', marker: null },
    ],
  };
}

describe('applyGrounding', () => {
  it('adds lookup chips, replaces the flag with an underline link, keeps markers', () => {
    const out = applyGrounding(TEXT, grounding());
    expect(out).toContain(`[ticket:${TK}]`);
    expect(out).toContain('There are 3 open tickets. [e2](#sfs-evidence-e2)');
    expect(out).toContain('[The runs are still dry runs](#sfs-unverified).');
    expect(out).not.toContain('(not verified)');
  });

  it('leaves text alone without a strict map or with a map that does not fit', () => {
    expect(applyGrounding(TEXT, null)).toBe(TEXT);
    expect(applyGrounding(TEXT, { ...grounding(), mode: 'fallback' })).toBe(TEXT);
    const broken = grounding();
    broken.sentences[0] = { ...broken.sentences[0], end: TEXT.length + 10 };
    expect(applyGrounding(TEXT, broken)).toBe(TEXT);
  });

  it('parses evidence hrefs', () => {
    expect(parseEvidenceHref('#sfs-evidence-e12')).toBe('e12');
    expect(parseEvidenceHref('#sfs-evidence-x')).toBeNull();
    expect(parseEvidenceHref('#sfs-cite-1')).toBeNull();
  });
});

describe('grounded answers in the drawer', () => {
  const turn: Turn = {
    id: 't1',
    role: 'assistant',
    content: TEXT,
    toolSteps: [],
    citations: [{ kind: 'ticket', ticket_id: TK, project_id: 'proj_1234567890abcdef', title: 'Enable live runs', at: null }],
    status: 'done',
    grounding: grounding(),
  };

  it('shows the source chip, a lookup chip and a quiet unverified underline', async () => {
    const { container } = render(
      <MemoryRouter>
        <AssistantMessage turn={turn} actions={actions} />
      </MemoryRouter>,
    );
    expect(screen.getByRole('link', { name: 'Source 1: Enable live runs' })).toBeInTheDocument();
    expect(screen.getByLabelText(/Source: .*ticket/i)).toBeInTheDocument();
    const underlined = container.querySelector('.decoration-dotted');
    expect(underlined).not.toBeNull();
    expect(underlined).toHaveTextContent('The runs are still dry runs');
    expect(underlined).toHaveTextContent('not verified: no source was found for this sentence');
    expect(container.textContent).not.toContain('dry runs (not verified)');
    expect(await axe(container)).toHaveNoViolations();
  });

  it('shows the plain text while streaming', () => {
    render(
      <MemoryRouter>
        <AssistantMessage turn={{ ...turn, status: 'streaming' }} actions={actions} />
      </MemoryRouter>,
    );
    expect(screen.getByText(/\(not verified\)/)).toBeInTheDocument();
  });
});
