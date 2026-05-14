import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import type { Persona, PersonaCreate, PersonaUpdate } from '../api/client';

/**
 * React-Query hooks for v0.10.1 Personas (Pro+).
 *
 * Endpoints:
 *   GET    /api/v1/projects/{id}/personas
 *   GET    /api/v1/projects/{id}/personas/{name}
 *   POST   /api/v1/projects/{id}/personas
 *   PUT    /api/v1/projects/{id}/personas/{name}
 *   DELETE /api/v1/projects/{id}/personas/{name}[?force=true]
 */

export function usePersonas(projectId: string | undefined) {
  const { auth } = useAuth();
  return useQuery<Persona[]>({
    queryKey: ['personas', projectId],
    queryFn: () => auth!.client.listPersonas(projectId!),
    enabled: !!auth && !!projectId,
    staleTime: 30_000,
  });
}

export function usePersona(projectId: string | undefined, name: string | undefined) {
  const { auth } = useAuth();
  return useQuery<Persona>({
    queryKey: ['persona', projectId, name],
    queryFn: () => auth!.client.getPersona(projectId!, name!),
    enabled: !!auth && !!projectId && !!name,
    staleTime: 30_000,
  });
}

export function useCreatePersona(projectId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: PersonaCreate) => auth!.client.createPersona(projectId!, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['personas', projectId] });
    },
  });
}

export function useUpdatePersona(projectId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: PersonaUpdate }) =>
      auth!.client.updatePersona(projectId!, name, body),
    onSuccess: (_data, { name }) => {
      void qc.invalidateQueries({ queryKey: ['personas', projectId] });
      void qc.invalidateQueries({ queryKey: ['persona', projectId, name] });
    },
  });
}

export function useDeletePersona(projectId: string | undefined) {
  const { auth } = useAuth();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ name, force }: { name: string; force?: boolean }) =>
      auth!.client.deletePersona(projectId!, name, force),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['personas', projectId] });
    },
  });
}
