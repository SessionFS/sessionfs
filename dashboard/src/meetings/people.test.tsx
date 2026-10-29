import { renderHook } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ApiError } from '../api/client';
import { useMeetingPeople } from './people';

/** The "people aren't listed" note names the scope the meeting lives in. */

const forbidden = new ApiError(403, JSON.stringify({ error: { code: '403', message: 'Forbidden' } }));
vi.mock('../hooks/useTickets', () => ({
  useAssignableUsers: (id?: string) => ({ data: undefined, isLoading: false, error: id ? forbidden : null }),
}));
vi.mock('../hooks/usePersonas', () => ({
  usePersonas: () => ({ data: [], isLoading: false }),
  useOrgPersonas: () => ({ data: [], isLoading: false }),
}));
vi.mock('../org/useOrgMembers', () => ({
  useOrgMembers: (id?: string) => ({ data: undefined, isLoading: false, error: id ? forbidden : null }),
}));

describe('useMeetingPeople', () => {
  it('words a 403 by scope', () => {
    const project = renderHook(() => useMeetingPeople({ kind: 'project', id: 'proj_1' }, 'org_1'));
    expect(project.result.current.peopleNote).toMatch(/view this project/);
    const group = renderHook(() => useMeetingPeople({ kind: 'group', id: 'pg_1' }, 'org_1'));
    expect(group.result.current.peopleNote).toMatch(/view this group/);
    expect(group.result.current.peopleNote).not.toMatch(/project/);
  });
});
