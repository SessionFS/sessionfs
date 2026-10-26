import { describe, expect, it } from 'vitest';
import { ticketActionAllowed } from './viewer';

const reader = { canEdit: false, userId: 'u_reader' };
const editor = { canEdit: true, userId: 'u_editor' };
const own = { created_by_user_id: 'u_reader', resolver_user_id: null };
const others = { created_by_user_id: 'u_owner', resolver_user_id: 'u_reader' };

describe('ticketActionAllowed', () => {
  it('lets an editor do everything', () => {
    for (const a of ['approve', 'dismiss', 'accept', 'reopen', 'close'] as const) {
      expect(ticketActionAllowed(a, others, editor)).toBe(true);
    }
  });

  it('never lets a reader approve or close', () => {
    expect(ticketActionAllowed('approve', own, reader)).toBe(false);
    expect(ticketActionAllowed('close', own, reader)).toBe(false);
  });

  it('lets a reader dismiss, reopen and accept their own ticket', () => {
    expect(ticketActionAllowed('dismiss', own, reader)).toBe(true);
    expect(ticketActionAllowed('reopen', own, reader)).toBe(true);
    expect(ticketActionAllowed('accept', own, reader)).toBe(true);
  });

  it('blocks self-review and other people’s tickets', () => {
    expect(ticketActionAllowed('accept', { ...own, resolver_user_id: 'u_reader' }, reader)).toBe(false);
    for (const a of ['dismiss', 'accept', 'reopen'] as const) {
      expect(ticketActionAllowed(a, others, reader)).toBe(false);
    }
  });
});
