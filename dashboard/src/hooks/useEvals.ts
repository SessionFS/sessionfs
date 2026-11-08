import { useMemo } from 'react';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type {
  ArtifactKind,
  ArtifactStage,
  EvalResultVerdict,
  EvalSuite,
  EvalRunState,
  EvalRunVerdict,
} from '../api/client';

/**
 * Evaluations + lineage (record-only: graders post results, SessionFS keeps
 * the evidence). The dashboard reads; the only writes are void, promote,
 * deprecate and family requirements, all done by a person.
 */

export interface EvalRunFilters {
  suite?: string;
  artifact_id?: string;
  state?: EvalRunState;
  verdict?: EvalRunVerdict;
}

export interface ArtifactFilters {
  kind?: ArtifactKind;
  stage?: ArtifactStage;
  include_deprecated?: boolean;
}

export function useEvalSuites(projectId: string) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['evalSuites', projectId],
    queryFn: () => auth!.client.listEvalSuites(projectId, { limit: 200 }),
    enabled: !!auth,
    staleTime: 30_000,
  });
}

export function useEvalSuite(projectId: string, suiteRef: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['evalSuite', projectId, suiteRef],
    queryFn: () => auth!.client.getEvalSuite(projectId, suiteRef!),
    enabled: !!auth && !!suiteRef,
    staleTime: 30_000,
  });
}

export function useEvalSuiteCases(projectId: string, suiteRef: string | null) {
  const { auth } = useAuth();
  return useInfiniteQuery({
    queryKey: ['evalSuiteCases', projectId, suiteRef],
    queryFn: ({ pageParam }) =>
      auth!.client.listEvalSuiteCases(projectId, suiteRef!, { cursor: pageParam || undefined, limit: 100 }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor || undefined,
    enabled: !!auth && !!suiteRef,
    staleTime: 30_000,
  });
}

export function useEvalRuns(projectId: string, filters: EvalRunFilters = {}, enabled = true) {
  const { auth } = useAuth();
  return useInfiniteQuery({
    queryKey: ['evalRuns', projectId, filters],
    queryFn: ({ pageParam }) =>
      auth!.client.listEvalRuns(projectId, { ...filters, cursor: pageParam || undefined, limit: 50 }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor || undefined,
    enabled: !!auth && enabled,
    staleTime: 15_000,
  });
}

export function useEvalRun(projectId: string, runId: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['evalRun', projectId, runId],
    queryFn: () => auth!.client.getEvalRun(projectId, runId!),
    enabled: !!auth && !!runId,
    staleTime: 15_000,
  });
}

export function useEvalResults(
  projectId: string,
  runId: string | null,
  filters: { verdict?: EvalResultVerdict; tag?: string } = {},
) {
  const { auth } = useAuth();
  return useInfiniteQuery({
    queryKey: ['evalResults', projectId, runId, filters],
    queryFn: ({ pageParam }) =>
      auth!.client.listEvalResults(projectId, runId!, { ...filters, cursor: pageParam || undefined, limit: 100 }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor || undefined,
    enabled: !!auth && !!runId,
    staleTime: 15_000,
  });
}

export function useEvalCompare(projectId: string, a: string | null, b: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['evalCompare', projectId, a, b],
    queryFn: () => auth!.client.compareEvalRuns(projectId, a!, b!),
    enabled: !!auth && !!a && !!b && a !== b,
    staleTime: 30_000,
  });
}

export function useVoidEvalRun(projectId: string) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { runId: string; reason: string }) => auth!.client.voidEvalRun(projectId, v.runId, v.reason),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['evalRuns', projectId] });
      void qc.invalidateQueries({ queryKey: ['evalRun', projectId] });
      void qc.invalidateQueries({ queryKey: ['artifactFamilyEvals', projectId] });
    },
  });
}

export function useArtifactFamilies(projectId: string) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['artifactFamilies', projectId],
    queryFn: () => auth!.client.listArtifactFamilies(projectId),
    enabled: !!auth,
    staleTime: 30_000,
  });
}

export function useArtifacts(projectId: string, filters: ArtifactFilters = {}) {
  const { auth } = useAuth();
  return useInfiniteQuery({
    queryKey: ['artifacts', projectId, filters],
    queryFn: ({ pageParam }) =>
      auth!.client.listArtifacts(projectId, { ...filters, cursor: pageParam || undefined, limit: 50 }),
    initialPageParam: '',
    getNextPageParam: (last) => last.next_cursor || undefined,
    enabled: !!auth,
    staleTime: 30_000,
  });
}

export function useArtifact(projectId: string, artifactId: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['artifact', projectId, artifactId],
    queryFn: () => auth!.client.getArtifact(projectId, artifactId!),
    enabled: !!auth && !!artifactId,
    staleTime: 30_000,
  });
}

export function useArtifactLineage(projectId: string, artifactId: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['artifactLineage', projectId, artifactId],
    queryFn: () => auth!.client.getArtifactLineage(projectId, artifactId!, { direction: 'both', depth: 6 }),
    enabled: !!auth && !!artifactId,
    staleTime: 30_000,
  });
}

export function usePromotionCheck(projectId: string, artifactId: string | null, toStage: ArtifactStage | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['promotionCheck', projectId, artifactId, toStage],
    queryFn: () => auth!.client.checkArtifactPromotion(projectId, artifactId!, toStage!),
    enabled: !!auth && !!artifactId && !!toStage,
    staleTime: 10_000,
  });
}

function invalidateArtifacts(qc: ReturnType<typeof useQueryClient>, projectId: string) {
  for (const key of ['artifacts', 'artifact', 'artifactFamilies', 'artifactLineage', 'promotionCheck', 'artifactFamilyEvals']) {
    void qc.invalidateQueries({ queryKey: [key, projectId] });
  }
}

export function usePromoteArtifact(projectId: string) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { artifactId: string; toStage: ArtifactStage }) =>
      auth!.client.promoteArtifact(projectId, v.artifactId, v.toStage),
    onSuccess: () => invalidateArtifacts(qc, projectId),
  });
}

export function useDeprecateArtifact(projectId: string) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (artifactId: string) => auth!.client.deprecateArtifact(projectId, artifactId),
    onSuccess: () => invalidateArtifacts(qc, projectId),
  });
}

export function useFamilyRequirements(projectId: string, kind: ArtifactKind | null, name: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['artifactFamilyRequirements', projectId, kind, name],
    queryFn: () => auth!.client.getArtifactFamilyRequirements(projectId, kind!, name!),
    enabled: !!auth && !!kind && !!name,
    staleTime: 30_000,
  });
}

export function useSaveFamilyRequirements(projectId: string) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: { kind: ArtifactKind; name: string; suites: string[]; require_tags: Record<string, string> }) =>
      auth!.client.putArtifactFamilyRequirements(projectId, v.kind, v.name, {
        suites: v.suites,
        require_tags: v.require_tags,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['artifactFamilyRequirements', projectId] });
      void qc.invalidateQueries({ queryKey: ['artifactFamilyEvals', projectId] });
      void qc.invalidateQueries({ queryKey: ['promotionCheck', projectId] });
    },
  });
}

export function useFamilyEvals(projectId: string, kind: ArtifactKind | null, name: string | null) {
  const { auth } = useAuth();
  return useQuery({
    queryKey: ['artifactFamilyEvals', projectId, kind, name],
    queryFn: () => auth!.client.getArtifactFamilyEvals(projectId, kind!, name!),
    enabled: !!auth && !!kind && !!name,
    staleTime: 30_000,
  });
}

/** lineage id → active suite, for naming runs and matrix columns. */
export function useSuiteNames(projectId: string): Map<string, EvalSuite> {
  const { data } = useEvalSuites(projectId);
  return useMemo(() => new Map((data?.suites ?? []).map((s) => [s.lineage_id, s])), [data]);
}

