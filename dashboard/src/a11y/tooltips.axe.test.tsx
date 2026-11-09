import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Badge, Button, IconButton, Tooltip } from '../components/ui';
import { Term, TermTip } from '../help/Term';
import { axe } from '../test/axe';

describe('tooltips, glossary terms and disabled reasons have no axe violations', () => {
  it('with every bubble closed', async () => {
    const { container } = render(
      <main>
        <h2 id="h">
          <Term id="review-loop">Review loop</Term>
        </h2>
        <section aria-labelledby="h">
          <p>
            3 <Term id="kb-claim">claims</Term> waiting to <Term id="kb-compile">compile</Term>.
          </p>
          <Term id="approval-advisory">
            <Badge label="Advisory" />
          </Term>
          <IconButton label="Close">x</IconButton>
          <Button disabled disabledReason="Only Suggested tickets can be approved">
            Approve
          </Button>
          <TermTip id="kb-rebuild">
            <Button variant="secondary">Rebuild</Button>
          </TermTip>
          <Tooltip content="Plain hint">
            <button type="button">Hint</button>
          </Tooltip>
        </section>
      </main>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it('with a glossary term and a disabled reason open', async () => {
    const { container } = render(
      <main>
        <h2>
          <Term id="merge-ready">Ready to merge</Term>
        </h2>
        <Button disabled disabledReason="Tick the confirmation box first">
          Execute merge
        </Button>
        <IconButton label="Delete" disabled disabledReason="Only admins can delete">
          x
        </IconButton>
      </main>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'Execute merge' }));
    fireEvent.click(screen.getByRole('button', { name: 'Ready to merge' }));
    expect(screen.getAllByRole('tooltip').length).toBeGreaterThan(0);
    expect(screen.getByRole('link', { name: /Learn more/ })).toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('with an icon button tooltip open from the keyboard', async () => {
    const { container } = render(
      <main>
        <IconButton label="Collapse sidebar">x</IconButton>
      </main>,
    );
    await userEvent.tab();
    expect(screen.getByRole('tooltip')).toHaveTextContent('Collapse sidebar');
    expect(await axe(container)).toHaveNoViolations();
  });
});
