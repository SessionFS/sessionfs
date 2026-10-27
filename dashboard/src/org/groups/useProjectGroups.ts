/**
 * React-Query hooks for project groups (GitHub #101).
 *
 * Query keys:
 *   ['orgGroups', orgId]            GET /orgs/{org}/groups
 *   ['projectGroup', orgId, id]     GET /orgs/{org}/groups/{id}
 *   ['myGroups']                    GET /me/groups
 *   ['projectGroups', projectId]    GET /projects/{id}/groups
 *
 * A group change can also change what the caller may see or edit (a project
 * joining a group the caller is granted on, a grant on themselves), so every
 * mutation refreshes the project list and project details too: their
 * `groups` and `can_edit` fields depend on group state.
 */

import { useMutation, useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';

import { useAuth } from '../../auth/AuthContext';
import type {
  ProjectGroupCreate,
  ProjectGroupDetail,
  ProjectGroupRole,
  ProjectGroupUpdate,
} from '../../api/client';

export const groupKeys = {
  org: (orgId: string | undefined) => ['orgGroups', orgId] as const,
  detail: (orgId: string | undefined, groupId: string | undefined) =>
    ['projectGroup', orgId, groupId] as const,
  mine: () => ['myGroups'] as const,
  project: (projectId: string | undefined) => ['projectGroups', projectId] as const,
};

function invalidateGroupState(qc: QueryClient, orgId: string | undefined) {
  void qc.invalidateQueries({ queryKey: groupKeys.org(orgId) });
  void qc.invalidateQueries({ queryKey: groupKeys.mine() });
  void qc.invalidateQueries({ queryKey: ['projectGroups'] });
  void qc.invalidateQueries({ queryKey: ['projects'] });
  void qc.invalidateQueries({ queryKey: ['project'] });
}

export function useOrgGroups(orgId: string | undefined) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: groupKeys.org(orgId),
    queryFn: () => auth!.client.listOrgGroups(orgId!),
    enabled: !!auth && !!orgId,
    staleTime: 30_000,
  });
}

export function useProjectGroup(orgId: string | undefined, groupId: string | undefined) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: groupKeys.detail(orgId, groupId),
    queryFn: () => auth!.client.getGroup(orgId!, groupId!),
    enabled: !!auth && !!orgId && !!groupId,
    // A 404 (deleted, or no standing) won't change on a retry.
    retry: false,
    staleTime: 15_000,
  });
}

export function useMyGroups(enabled = true) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: groupKeys.mine(),
    queryFn: () => auth!.client.listMyGroups(),
    enabled: !!auth && enabled,
    staleTime: 60_000,
  });
}

export function useProjectGroupsOf(projectId: string | undefined, enabled = true) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: groupKeys.project(projectId),
    queryFn: () => auth!.client.listProjectGroups(projectId!),
    enabled: !!auth && !!projectId && enabled,
    staleTime: 60_000,
  });
}

export function useCreateGroup(orgId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ProjectGroupCreate) => auth!.client.createGroup(orgId!, body),
    onSuccess: (group) => {
      qc.setQueryData(groupKeys.detail(orgId, group.id), group);
      void qc.invalidateQueries({ queryKey: groupKeys.detail(orgId, group.id) });
      invalidateGroupState(qc, orgId);
    },
  });
}

/** Mutations on one group: each response is the fresh detail, seeded into its cache entry. */
function useGroupMutation<V>(
  orgId: string | undefined,
  groupId: string | undefined,
  fn: (vars: V) => Promise<ProjectGroupDetail>,
) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: fn,
    onSuccess: (detail) => {
      if (detail) qc.setQueryData(groupKeys.detail(orgId, groupId), detail);
      // Overlapping mutations can resolve out of order, so the detail just
      // written may already be stale: mark it for a refetch as well.
      void qc.invalidateQueries({ queryKey: groupKeys.detail(orgId, groupId) });
      invalidateGroupState(qc, orgId);
    },
  });
}

export function useUpdateGroup(orgId: string | undefined, groupId: string | undefined) {
  const { auth } = useAuth();
  return useGroupMutation(orgId, groupId, (body: ProjectGroupUpdate) =>
    auth!.client.updateGroup(orgId!, groupId!, body),
  );
}

export function useDeleteGroup(orgId: string | undefined, groupId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => auth!.client.deleteGroup(orgId!, groupId!),
    onSuccess: () => {
      qc.removeQueries({ queryKey: groupKeys.detail(orgId, groupId) });
      invalidateGroupState(qc, orgId);
    },
  });
}

export function useGrantGroupMember(orgId: string | undefined, groupId: string | undefined) {
  const { auth } = useAuth();
  return useGroupMutation(orgId, groupId, ({ userId, role }: { userId: string; role: ProjectGroupRole }) =>
    auth!.client.grantGroupMember(orgId!, groupId!, userId, role),
  );
}

export function useRevokeGroupMember(orgId: string | undefined, groupId: string | undefined) {
  const { auth } = useAuth();
  return useGroupMutation(orgId, groupId, (userId: string) =>
    auth!.client.revokeGroupMember(orgId!, groupId!, userId),
  );
}

export function useAddGroupProject(orgId: string | undefined, groupId: string | undefined) {
  const { auth } = useAuth();
  return useGroupMutation(orgId, groupId, (projectId: string) =>
    auth!.client.addGroupProject(orgId!, groupId!, projectId),
  );
}

export function useRemoveGroupProject(orgId: string | undefined, groupId: string | undefined) {
  const { auth } = useAuth();
  return useGroupMutation(orgId, groupId, (projectId: string) =>
    auth!.client.removeGroupProject(orgId!, groupId!, projectId),
  );
}
