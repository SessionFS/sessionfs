import { beforeEach, describe, expect, it, vi } from 'vitest';

import { activeOption, apiScope, scopeKey, scopeOptions } from './scope';
import { clearRememberedScope, readRememberedScope, rememberScope, scopeOwner } from './scopeMemory';

describe('helper scope', () => {
  it('sends a group scope on its own', () => {
    expect(apiScope({ group_id: 'g1', label: 'Payments' })).toEqual({ group_id: 'g1' });
    expect(apiScope({ project_id: 'p1', label: 'Acme' })).toEqual({ project_id: 'p1', session_ids: undefined });
    expect(apiScope({ session_ids: [] })).toBeUndefined();
    expect(apiScope(undefined)).toBeUndefined();
    expect(scopeKey({ group_id: 'g1' })).toBe('group:g1');
  });

  it('lists entry, remembered, all, then groups without duplicates', () => {
    const opts = scopeOptions(
      { project_id: 'p1', label: 'Acme' },
      { project_id: 'p1' },
      { remembered: { group_id: 'g1' }, groups: [{ id: 'g1', name: 'Payments', project_count: 2 }, { id: 'g2', name: 'Ops' }] },
    );
    expect(opts.map((o) => [o.label, o.section ?? null])).toEqual([
      ['Acme', null],
      ['Payments', null],
      ['All my sessions', null],
      ['Ops', 'Project groups'],
    ]);
  });

  it('labels a restored group scope from the groups list', () => {
    expect(activeOption(undefined, { group_id: 'g2' }, { groups: [{ id: 'g2', name: 'Ops' }] }).label).toBe('Ops');
    expect(activeOption(undefined, { group_id: 'g3' }).label).toBe('This project group');
  });
});

describe('remembered scope', () => {
  const A = scopeOwner('https://api.example', 'sk_sfs_user_a');
  const B = scopeOwner('https://api.example', 'sk_sfs_user_b');
  beforeEach(() => clearRememberedScope());

  it('round-trips a scope and "All my sessions"', () => {
    expect(readRememberedScope(A)).toBeUndefined();
    rememberScope(A, { project_id: 'p1', label: 'Acme', session_ids: ['x'] });
    expect(readRememberedScope(A)).toEqual({ project_id: 'p1', label: 'Acme' });
    rememberScope(A, undefined);
    expect(readRememberedScope(A)).toEqual({});
  });

  it('belongs to the identity that stored it (another sign-in in the tab sees nothing)', () => {
    expect(A).not.toBe(B);
    rememberScope(A, { group_id: 'g1', label: 'Payments' });
    expect(readRememberedScope(B)).toBeUndefined();
    expect(readRememberedScope('')).toBeUndefined();
    expect(JSON.stringify(window.sessionStorage.getItem('sfs.helper.lastScope'))).not.toContain('sk_sfs');
  });

  it('still works for the page load when storage throws, and clears both copies', () => {
    const spy = vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => {
      throw new Error('blocked');
    });
    try {
      rememberScope(A, { group_id: 'g1', label: 'Payments' });
      expect(readRememberedScope(A)).toEqual({ group_id: 'g1', label: 'Payments' });
      clearRememberedScope();
      expect(readRememberedScope(A)).toBeUndefined();
    } finally {
      spy.mockRestore();
    }
  });
});
