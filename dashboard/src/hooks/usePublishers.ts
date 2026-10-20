import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../auth/AuthContext';
import { useMe } from './useMe';
import type {
  AddTrustedPublisherInput,
  ClaimPublisherInput,
  StartVerificationInput,
} from '../api/publisherTypes';

/** Publisher management is identity-scoped: keys carry the signed-in user id. */
function useIdentity(): string | null {
  const me = useMe();
  return me.data?.user_id ?? null;
}

export function useMyPublishers() {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useQuery({
    queryKey: ['publishers', identity],
    queryFn: () => auth!.client.listMyPublishers(),
    enabled: !!auth && !!identity,
  });
}

export function usePublisherListings(publisherId: string | null) {
  const { auth } = useAuth();
  const identity = useIdentity();
  return useQuery({
    queryKey: ['publisher-listings', identity, publisherId],
    queryFn: () => auth!.client.listPublisherListings(publisherId!),
    enabled: !!auth && !!identity && !!publisherId,
  });
}

function useInvalidate() {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ['publishers'] });
    void qc.invalidateQueries({ queryKey: ['publisher-listings'] });
  };
}

export function useClaimPublisher() {
  const { auth } = useAuth();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (body: ClaimPublisherInput) => auth!.client.claimPublisher(body),
    onSuccess: invalidate,
  });
}

export function useStartVerification() {
  const { auth } = useAuth();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ publisherId, body }: { publisherId: string; body: StartVerificationInput }) =>
      auth!.client.startPublisherVerification(publisherId, body),
    onSuccess: invalidate,
  });
}

export function useCheckVerification() {
  const { auth } = useAuth();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ publisherId, verificationId }: { publisherId: string; verificationId: string }) =>
      auth!.client.checkPublisherVerification(publisherId, verificationId),
    onSuccess: invalidate,
  });
}

export function useRevokeVerification() {
  const { auth } = useAuth();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ publisherId, verificationId }: { publisherId: string; verificationId: string }) =>
      auth!.client.revokePublisherVerification(publisherId, verificationId),
    onSuccess: invalidate,
  });
}

export function useAddTrustedPublisher() {
  const { auth } = useAuth();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ publisherId, body }: { publisherId: string; body: AddTrustedPublisherInput }) =>
      auth!.client.addTrustedPublisher(publisherId, body),
    onSuccess: invalidate,
  });
}

export function useRemoveTrustedPublisher() {
  const { auth } = useAuth();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ publisherId, trustedId }: { publisherId: string; trustedId: string }) =>
      auth!.client.removeTrustedPublisher(publisherId, trustedId),
    onSuccess: invalidate,
  });
}
