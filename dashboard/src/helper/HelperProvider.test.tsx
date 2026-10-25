import { render, renderHook, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { HelperProvider, useHelper } from './HelperProvider';

/**
 * The helper drawer is provided at the app root, but screens are often
 * rendered in isolation (tests, previews). useHelper must not throw when the
 * provider is missing — it returns an undefined openHelper so consumers can
 * hide their "Ask the helper" entry points. When a provider is present, the
 * drawer also remembers its trigger so focus returns there on close.
 */

// The real HelperPanel needs the full auth/status/query tree; for the focus
// contract we only care that the drawer's onClose hands back to the provider.
vi.mock('./HelperPanel', () => ({
  default: ({ open, onClose }: { open: boolean; onClose: () => void }) =>
    open ? (
      <div>
        <button onClick={onClose}>Close drawer</button>
      </div>
    ) : null,
}));

function Trigger() {
  const { openHelper } = useHelper();
  return <button onClick={() => openHelper?.()}>Ask the helper</button>;
}

describe('useHelper', () => {
  it('returns a safe default outside a HelperProvider', () => {
    const { result } = renderHook(() => useHelper());
    expect(result.current.openHelper).toBeUndefined();
  });

  it('lets consumers hide entry points when the provider is absent', () => {
    function Consumer() {
      const { openHelper } = useHelper();
      return openHelper ? <button>Ask the helper</button> : <span>no helper</span>;
    }
    render(<Consumer />);
    expect(screen.queryByRole('button', { name: 'Ask the helper' })).toBeNull();
    expect(screen.getByText('no helper')).toBeInTheDocument();
  });

  it('restores focus to the trigger when the drawer closes', async () => {
    const user = userEvent.setup();
    render(
      <HelperProvider>
        <Trigger />
      </HelperProvider>,
    );

    const trigger = screen.getByRole('button', { name: 'Ask the helper' });
    await user.click(trigger);

    // The drawer is open and its own control now holds focus.
    const close = screen.getByRole('button', { name: 'Close drawer' });
    expect(close).toBeInTheDocument();

    await user.click(close);

    // Focus returns to the element that opened the drawer, not <body>.
    expect(trigger).toHaveFocus();
  });
});
