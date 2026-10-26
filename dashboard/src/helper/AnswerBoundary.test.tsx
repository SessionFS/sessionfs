import type { ReactElement } from 'react';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';

import AnswerBoundary from './AnswerBoundary';
import { AssistantMessage } from './HelperMessage';
import type { Turn } from './format';

vi.mock('../hooks/useProjects', () => ({ useProject: () => ({ data: undefined }) }));

const actions = { onNewConversation: () => {}, onNavigate: () => {} };

function turn(content: string): Turn {
  return { id: 't1', role: 'assistant', content, toolSteps: [], citations: [], status: 'done' };
}

describe('helper answer rendering', () => {
  it('renders hostile ref hrefs as plain text instead of crashing', () => {
    const content = ['constructor', 'toString', '__proto__', 'valueOf', 'hasOwnProperty']
      .map((k) => `[${k}](#sfs-ref-${k}-a)`)
      .join(' ');
    render(
      <MemoryRouter>
        <AssistantMessage turn={turn(`Answer ${content}`)} actions={actions} />
      </MemoryRouter>,
    );
    expect(screen.getByText(/Answer/)).toBeInTheDocument();
    expect(screen.queryByText('This answer couldn’t be displayed.')).toBeNull();
  });

  it('contains a render error to the one answer and recovers when the text changes', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    function Boom({ text }: { text: string }): ReactElement {
      if (text === 'bad') throw new Error('boom');
      return <p>{text}</p>;
    }
    const { rerender } = render(
      <div>
        <p>Drawer chrome</p>
        <AnswerBoundary resetKey="bad">
          <Boom text="bad" />
        </AnswerBoundary>
      </div>,
    );
    expect(screen.getByText('This answer couldn’t be displayed.')).toBeInTheDocument();
    expect(screen.getByText('Drawer chrome')).toBeInTheDocument();
    rerender(
      <div>
        <p>Drawer chrome</p>
        <AnswerBoundary resetKey="good">
          <Boom text="good" />
        </AnswerBoundary>
      </div>,
    );
    expect(screen.getByText('good')).toBeInTheDocument();
    spy.mockRestore();
  });
});
