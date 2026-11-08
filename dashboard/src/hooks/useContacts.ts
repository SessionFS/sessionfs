import { keepPreviousData, useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, type ContactListParams, type ExternalContactCreate, type ExternalContactUpdate } from '../api/client';
import { useAuth } from '../auth/AuthContext';

/** React-Query hooks for an org's external contacts (GitHub #125). */

const ROOT = 'contacts';
const SUGGESTIONS = 'contact-suggestions';

/** A 4xx (unknown contact, not a member, no access) won't change on a retry. */
const noRetryOn4xx = (count: number, err: unknown) => !(err instanceof ApiError && err.status < 500) && count < 1;

export function useContactList(orgId: string | undefined, params: Omit<ContactListParams, 'before'>) {
  const { auth } = useAuth();
  return useInfiniteQuery({
    queryKey: [ROOT, orgId, 'list', params],
    queryFn: ({ pageParam }) => auth!.client.listContacts(orgId!, { ...params, before: pageParam }),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_before ?? undefined,
    enabled: !!auth && !!orgId,
    placeholderData: keepPreviousData,
    retry: noRetryOn4xx,
    staleTime: 15_000,
  });
}

/** Active contacts for a picker (one page of 200, the server's max). */
export function useActiveContacts(orgId: string | null | undefined) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [ROOT, orgId, 'active'],
    queryFn: () => auth!.client.listContacts(orgId!, { status: 'active', limit: 200 }),
    enabled: !!auth && !!orgId,
    retry: noRetryOn4xx,
    staleTime: 30_000,
  });
}

export function useContact(orgId: string | undefined, contactId: string | null | undefined) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [ROOT, orgId, 'detail', contactId],
    queryFn: () => auth!.client.getContact(orgId!, contactId!),
    enabled: !!auth && !!orgId && !!contactId,
    retry: noRetryOn4xx,
  });
}

export function useContactReferences(orgId: string | undefined, contactId: string | null | undefined) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [ROOT, orgId, 'references', contactId],
    queryFn: () => auth!.client.getContactReferences(orgId!, contactId!),
    enabled: !!auth && !!orgId && !!contactId,
    retry: noRetryOn4xx,
  });
}

export function useContactSuggestions(orgId: string | undefined, enabled = true) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: [SUGGESTIONS, orgId],
    queryFn: () => auth!.client.getContactSuggestions(orgId!),
    enabled: !!auth && !!orgId && enabled,
    retry: noRetryOn4xx,
    staleTime: 15_000,
  });
}

/** Contacts changed: lists, details, references, and anything that renders one. */
function useContactsChanged(orgId: string | undefined) {
  const qc = useQueryClient();
  return (opts: { meetings?: boolean; tickets?: boolean } = {}) => {
    void qc.invalidateQueries({ queryKey: [ROOT, orgId] });
    void qc.invalidateQueries({ queryKey: [SUGGESTIONS, orgId] });
    if (opts.meetings) {
      void qc.invalidateQueries({ queryKey: ['meeting'] });
      void qc.invalidateQueries({ queryKey: ['meetings'] });
    }
    if (opts.tickets) {
      void qc.invalidateQueries({ queryKey: ['tickets'] });
      void qc.invalidateQueries({ queryKey: ['ticket'] });
    }
  };
}

export function useCreateContact(orgId: string | undefined) {
  const { auth } = useAuth();
  const changed = useContactsChanged(orgId);
  return useMutation({
    mutationFn: (body: ExternalContactCreate) => auth!.client.createContact(orgId!, body),
    onSuccess: () => changed(),
  });
}

export function useUpdateContact(orgId: string | undefined) {
  const { auth } = useAuth();
  const changed = useContactsChanged(orgId);
  return useMutation({
    mutationFn: ({ contactId, body }: { contactId: string; body: ExternalContactUpdate }) =>
      auth!.client.updateContact(orgId!, contactId, body),
    // A rename or archive shows on meetings and tickets that render it.
    onSuccess: () => changed({ meetings: true, tickets: true }),
  });
}

export function useContactAlias(orgId: string | undefined) {
  const { auth } = useAuth();
  const changed = useContactsChanged(orgId);
  return useMutation({
    mutationFn: ({ contactId, alias, remove = false }: { contactId: string; alias: string; remove?: boolean }) =>
      remove
        ? auth!.client.removeContactAlias(orgId!, contactId, alias)
        : auth!.client.addContactAlias(orgId!, contactId, alias),
    onSuccess: () => changed(),
  });
}

export function useMergeContacts(orgId: string | undefined) {
  const { auth } = useAuth();
  const changed = useContactsChanged(orgId);
  return useMutation({
    mutationFn: ({ targetId, sourceId, confirm }: { targetId: string; sourceId: string; confirm: boolean }) =>
      auth!.client.mergeContacts(orgId!, targetId, sourceId, confirm),
    onSuccess: (result) => {
      if (result.confirmed) changed({ meetings: true, tickets: true });
    },
  });
}

export function useDeleteContact(orgId: string | undefined) {
  const { auth } = useAuth();
  const changed = useContactsChanged(orgId);
  return useMutation({
    mutationFn: (contactId: string) => auth!.client.deleteContact(orgId!, contactId),
    onSuccess: () => changed({ meetings: true, tickets: true }),
  });
}

export function useApplyContactSuggestion(orgId: string | undefined) {
  const { auth } = useAuth();
  const changed = useContactsChanged(orgId);
  return useMutation({
    mutationFn: ({ name, contactId }: { name: string; contactId: string }) =>
      auth!.client.applyContactSuggestion(orgId!, name, contactId),
    onSuccess: () => changed({ meetings: true }),
  });
}

export function useDismissContactSuggestion(orgId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => auth!.client.dismissContactSuggestion(orgId!, name),
    onSuccess: () => void qc.invalidateQueries({ queryKey: [SUGGESTIONS, orgId] }),
  });
}

/** Contacts whose name or alias matches `q` (any status), for duplicate hints. */
export function useContactLookup(orgId: string | null | undefined, q: string) {
  const { auth } = useAuth();
  const term = q.trim();
  return useQuery({
    queryKey: [ROOT, orgId, 'lookup', term],
    queryFn: () => auth!.client.listContacts(orgId!, { q: term, status: 'all', limit: 10 }),
    enabled: !!auth && !!orgId && term.length >= 2,
    retry: noRetryOn4xx,
    staleTime: 15_000,
  });
}
