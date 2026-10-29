import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Dialog } from '../components/ui/Dialog';
import { Drawer } from '../components/ui/Drawer';

/**
 * A dialog opened from a drawer: both focus traps are active. A stray Tab
 * (focus on a control just disabled, or on <body>) belongs to the newest
 * trap only; once the dialog closes the drawer handles it again.
 */
function Harness({ dialogOpen }: { dialogOpen: boolean }) {
  const [saving, setSaving] = useState(false);
  return (
    <Drawer open onClose={() => {}} titleId="drawer-title">
      <h2 id="drawer-title">Helper</h2>
      <button type="button">Drawer action</button>
      <Dialog open={dialogOpen} onClose={() => {}} titleId="dialog-title">
        <h2 id="dialog-title">Confirm</h2>
        <button type="button" disabled={saving} onClick={() => setSaving(true)}>
          Save
        </button>
      </Dialog>
    </Drawer>
  );
}

describe('nested focus traps', () => {
  it('lets only the newest trap rescue a stray Tab, then the drawer again once the dialog closes', async () => {
    const user = userEvent.setup();
    // The drawer is open first; the dialog is opened from it later (as in
    // the helper), so the drawer's trap was activated before the dialog's.
    const { rerender } = render(<Harness dialogOpen={false} />);
    rerender(<Harness dialogOpen />);
    const dialog = screen.getByRole('dialog', { name: 'Confirm' });
    await user.click(screen.getByRole('button', { name: 'Save' }));
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();

    await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(screen.getByRole('button', { name: 'Drawer action' })).not.toHaveFocus();

    // A browser moves focus off a button disabled while focused, onto
    // <body>, so the next Tab never passes through the dialog's container.
    (document.activeElement as HTMLElement | null)?.blur();
    await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(screen.getByRole('button', { name: 'Drawer action' })).not.toHaveFocus();

    // The dialog closes: the drawer's trap answers again.
    rerender(<Harness dialogOpen={false} />);
    (document.activeElement as HTMLElement | null)?.blur();
    await user.tab();
    expect(screen.getByRole('dialog', { name: 'Helper' }).contains(document.activeElement)).toBe(true);
  });

  it('gives the focusable dialog panel a visible focus ring from the brand token', () => {
    render(<Harness dialogOpen />);
    const panel = screen.getByRole('dialog', { name: 'Confirm' });
    expect(panel).toHaveAttribute('tabindex', '-1');
    expect(panel.className).toMatch(/focus-visible:shadow-\[inset_0_0_0_3px_var\(--brand-glow\)/);
  });
});
