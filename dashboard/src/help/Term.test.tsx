import { act, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Term } from './Term';
import { getGlossaryEntry } from './glossary';
import { axe } from '../test/axe';

const entry = getGlossaryEntry('kb-claim')!;

afterEach(() => {
  vi.useRealTimers();
});

describe('Term', () => {
  it('renders the label as a button described by the definition', () => {
    render(
      <p>
        Five <Term id="kb-claim">claims</Term> waiting
      </p>,
    );
    const btn = screen.getByRole('button', { name: 'claims' });
    expect(btn).toHaveAccessibleDescription(entry.definition);
    expect(btn).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('link', { name: /learn more/i })).not.toBeInTheDocument();
  });

  it('defaults the label to the glossary term', () => {
    render(<Term id="kb-claim" />);
    expect(screen.getByRole('button', { name: entry.term })).toBeInTheDocument();
  });

  it('opens on hover after a short delay and closes when the pointer leaves', () => {
    vi.useFakeTimers();
    render(<Term id="kb-claim" />);
    const wrap = screen.getByRole('button').parentElement!;
    fireEvent.pointerEnter(wrap, { pointerType: 'mouse' });
    expect(screen.queryByText(/Learn more/)).not.toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(350);
    });
    expect(screen.getByRole('link', { name: /Learn more about Claim/ })).toHaveAttribute(
      'href',
      'https://sessionfs.dev/knowledge-base/',
    );
    expect(screen.getByRole('button')).toHaveAttribute('aria-expanded', 'true');
    fireEvent.pointerLeave(wrap, { pointerType: 'mouse' });
    act(() => {
      vi.advanceTimersByTime(200);
    });
    expect(screen.queryByText(/Learn more/)).not.toBeInTheDocument();
  });

  it('ignores hover from touch input', () => {
    vi.useFakeTimers();
    render(<Term id="kb-claim" />);
    fireEvent.pointerEnter(screen.getByRole('button').parentElement!, { pointerType: 'touch' });
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.queryByText(/Learn more/)).not.toBeInTheDocument();
  });

  it('opens on keyboard focus, lets Tab reach the link, and closes on Escape', async () => {
    const user = userEvent.setup();
    render(
      <div>
        <Term id="kb-claim" />
        <button type="button">After</button>
      </div>,
    );
    await user.tab();
    const trigger = screen.getByRole('button', { name: entry.term });
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    await user.tab();
    expect(screen.getByRole('link', { name: /Learn more/ })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByText(/Learn more/)).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('closes when focus moves past it', async () => {
    const user = userEvent.setup();
    render(
      <div>
        <Term id="kb-claim" />
        <button type="button">After</button>
      </div>,
    );
    await user.tab();
    await user.tab(); // the link
    await user.tab(); // After
    expect(screen.getByRole('button', { name: 'After' })).toHaveFocus();
    expect(screen.queryByText(/Learn more/)).not.toBeInTheDocument();
  });

  it('toggles on tap and closes on a tap elsewhere', () => {
    render(
      <div>
        <Term id="kb-claim" />
        <span>elsewhere</span>
      </div>,
    );
    const trigger = screen.getByRole('button', { name: entry.term });
    fireEvent.click(trigger);
    expect(screen.getByText(/Learn more/)).toBeInTheDocument();
    fireEvent.click(trigger);
    expect(screen.queryByText(/Learn more/)).not.toBeInTheDocument();
    fireEvent.click(trigger);
    fireEvent.pointerDown(screen.getByText('elsewhere'));
    expect(screen.queryByText(/Learn more/)).not.toBeInTheDocument();
  });

  it('claims Escape before a dialog underneath sees it', () => {
    const onDialogKey = vi.fn();
    render(
      <div onKeyDown={onDialogKey}>
        <Term id="kb-claim" />
      </div>,
    );
    const trigger = screen.getByRole('button', { name: entry.term });
    fireEvent.click(trigger);
    fireEvent.keyDown(trigger, { key: 'Escape' });
    expect(onDialogKey).not.toHaveBeenCalled();
    expect(screen.queryByText(/Learn more/)).not.toBeInTheDocument();
  });

  it('warns in development when nested inside another control', () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(
      <a href="/x">
        <Term id="kb-claim" />
      </a>,
    );
    expect(err).toHaveBeenCalledWith(expect.stringContaining('inside an interactive element'));
    err.mockRestore();
  });

  it('has no axe violations, open or closed', async () => {
    const { container } = render(
      <main>
        <h2>
          <Term id="review-loop">Review loop</Term>
        </h2>
        <table>
          <thead>
            <tr>
              <th>
                <Term id="eval-grader">Grader</Term>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>a</td>
            </tr>
          </tbody>
        </table>
      </main>,
    );
    expect(await axe(container)).toHaveNoViolations();
    fireEvent.click(screen.getByRole('button', { name: 'Review loop' }));
    expect(await axe(container)).toHaveNoViolations();
  });
});
