import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';

export function useFolders() {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['bookmark-folders'],
    queryFn: () => auth!.client.listFolders(),
    enabled: !!auth,
    // 5 min — folders are user-configured and rarely change mid-session.
    // SessionList eager-loads this to show the folder sidebar + bookmark
    // counts; the longer window cuts redundant refetches on every
    // remount / tab focus. Mutations (create/update/delete/bookmark)
    // already invalidate the cache, so stale data isn't a correctness
    // risk.
    staleTime: 300_000,
  });
}

export function useCreateFolder() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, color }: { name: string; color?: string }) =>
      auth!.client.createFolder(name, color),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['bookmark-folders'] }),
  });
}

export function useUpdateFolder() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ folderId, updates }: { folderId: string; updates: { name?: string; color?: string } }) =>
      auth!.client.updateFolder(folderId, updates),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['bookmark-folders'] }),
  });
}

export function useDeleteFolder() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (folderId: string) => auth!.client.deleteFolder(folderId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['bookmark-folders'] }),
  });
}

export function useAddBookmark() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ folderId, sessionId }: { folderId: string; sessionId: string }) =>
      auth!.client.addBookmark(folderId, sessionId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bookmark-folders'] });
      qc.invalidateQueries({ queryKey: ['folder-sessions'] });
    },
  });
}

export function useRemoveBookmark() {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (bookmarkId: string) => auth!.client.removeBookmark(bookmarkId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bookmark-folders'] });
      qc.invalidateQueries({ queryKey: ['folder-sessions'] });
    },
  });
}

export function useFolderSessions(folderId: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['folder-sessions', folderId],
    queryFn: () => auth!.client.listFolderSessions(folderId!),
    enabled: !!auth && !!folderId,
    staleTime: 30_000,
  });
}
