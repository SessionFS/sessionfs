/**
 * React-Query hooks for the helper's model settings, custom instructions and
 * token usage: personal (`/api/v1/settings/helper`) and org
 * (`/api/v1/orgs/{org_id}/helper-settings`). PUT merges on the server, so the
 * forms send only the fields that changed; the response replaces the cache.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { useAuth } from '../auth/AuthContext';
import type {
  OrgHelperSettings,
  OrgHelperSettingsUpdate,
  UserHelperSettings,
  UserHelperSettingsUpdate,
} from '../api/client';

export const userHelperSettingsKey = ['userHelperSettings'] as const;
export const orgHelperSettingsKey = (orgId: string | undefined) => ['orgHelperSettings', orgId] as const;

export function useUserHelperSettings() {
  const { auth } = useAuth();
  return useQuery<UserHelperSettings>({
    queryKey: userHelperSettingsKey,
    queryFn: () => auth!.client.getUserHelperSettings(),
    enabled: !!auth,
    staleTime: 30_000,
    // A 403 means the plan lacks the helper; retrying won't change that.
    retry: false,
  });
}

export function useSaveUserHelperSettings() {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: UserHelperSettingsUpdate) => auth!.client.saveUserHelperSettings(body),
    onSuccess: (data) => {
      queryClient.setQueryData(userHelperSettingsKey, data);
      // The drawer's status carries the effective settings.
      void queryClient.invalidateQueries({ queryKey: ['helperStatus'] });
    },
  });
}

export function useOrgHelperSettings(orgId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<OrgHelperSettings>({
    queryKey: orgHelperSettingsKey(orgId),
    queryFn: () => auth!.client.getOrgHelperSettings(orgId!),
    enabled: !!auth && !!orgId,
    staleTime: 30_000,
  });
}

export function useSaveOrgHelperSettings(orgId: string | undefined) {
  const { auth } = useAuth();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: OrgHelperSettingsUpdate) => auth!.client.saveOrgHelperSettings(orgId!, body),
    onSuccess: (data) => {
      queryClient.setQueryData(orgHelperSettingsKey(orgId), data);
      void queryClient.invalidateQueries({ queryKey: ['helperStatus'] });
    },
  });
}
