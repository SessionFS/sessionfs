import { render, screen, act } from '@testing-library/react';
import { useContext } from 'react';
import { describe, expect, it } from 'vitest';
import { ToastProvider, ToastContext } from './Toast';

function Trigger() {
  const ctx = useContext(ToastContext)!;
  return (
    <>
      <button onClick={() => ctx.addToast('error', 'It broke')}>err</button>
      <button onClick={() => ctx.addToast('success', 'Saved')}>ok</button>
    </>
  );
}

describe('Toast roles', () => {
  it('keeps both live regions mounted before any toast appears', () => {
    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>,
    );
    expect(screen.getByRole('alert')).toBeEmptyDOMElement();
    expect(screen.getByRole('status')).toBeEmptyDOMElement();
  });

  it('announces errors assertively and others politely', async () => {
    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>,
    );
    await act(async () => {
      screen.getByText('err').click();
      screen.getByText('ok').click();
    });
    expect(screen.getByRole('alert')).toHaveTextContent('It broke');
    expect(screen.getByRole('status')).toHaveTextContent('Saved');
    expect(screen.getByRole('alert')).not.toHaveTextContent('Saved');
    expect(screen.getAllByRole('button', { name: 'Dismiss notification' })).toHaveLength(2);
  });
});
