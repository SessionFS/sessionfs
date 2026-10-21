import { useQuery } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { useMe } from './useMe';

/** Native per-tool catalogs visible to the signed-in identity (marketplace v2 §7.6). */
export function useNativeCatalogs(enabled = true) {
  const { auth } = useAuth();
  const me = useMe();
  const identity = me.data?.user_id ?? null;
  return useQuery({
    queryKey: ['native-catalogs', identity],
    queryFn: () => auth!.client.listNativeCatalogs(),
    enabled: !!auth && !!identity && enabled,
    staleTime: 5 * 60_000,
  });
}

/** Per-tool works-in evidence for one version. */
export function useWorksIn(versionId: string | undefined, enabled = true) {
  const { auth } = useAuth();
  const me = useMe();
  const identity = me.data?.user_id ?? null;
  return useQuery({
    queryKey: ['works-in', identity, versionId],
    queryFn: () => auth!.client.getWorksIn(versionId!),
    enabled: !!auth && !!identity && !!versionId && enabled,
    staleTime: 60_000,
    retry: false,
  });
}
