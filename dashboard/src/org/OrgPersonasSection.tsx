/**
 * GH #70 — org-level personas, managed on the Organization page.
 *
 * An org persona is usable from every project in the organization; a
 * project persona with the same name overrides it inside that project.
 * Admins and owners manage every org persona and choose which member each
 * one represents. GitHub #81 — any other member sees only the personas that
 * represent them and may edit their role, content and specializations (the
 * server enforces the same split).
 */

import { useState } from 'react';
import { useToast } from '../hooks/useToast';
import {
  useCreateOrgPersona,
  useDeleteOrgPersona,
  useOrgPersonas,
  useUpdateOrgPersona,
} from '../hooks/usePersonas';
import RelativeDate from '../components/RelativeDate';
import { type Persona } from '../api/client';
import { Button, Select, Table } from '../components/ui';
import { DeleteConfirmModal, PersonaEditModal, type EditState } from '../projects/PersonasTab';
import { errorMessage } from '../utils/errorMessage';

export interface OrgPersonaMember {
  user_id: string;
  email: string;
}

interface OrgPersonasSectionProps {
  orgId: string;
  /** Defaults to true: the section used to render for admins only. */
  isAdmin?: boolean;
  myUserId?: string;
  members?: OrgPersonaMember[];
  /** Wrapper classes; the section renders nothing for a member with no persona. */
  className?: string;
}

export default function OrgPersonasSection({
  orgId,
  isAdmin = true,
  myUserId,
  members = [],
  className,
}: OrgPersonasSectionProps) {
  const { data: allPersonas, isLoading, error } = useOrgPersonas(orgId);
  const create = useCreateOrgPersona(orgId);
  const update = useUpdateOrgPersona(orgId);
  const remove = useDeleteOrgPersona(orgId);
  const { addToast } = useToast();

  const [edit, setEdit] = useState<EditState | null>(null);
  const [deleting, setDeleting] = useState<Persona | null>(null);

  const data = isAdmin
    ? allPersonas
    : allPersonas?.filter((p) => !!myUserId && p.represented_user_id === myUserId);
  // A member who represents no persona has nothing to see here.
  if (!isAdmin && (!data || data.length === 0)) return null;

  const memberOptions = [
    { value: '', label: 'Nobody' },
    ...members.map((m) => ({ value: m.user_id, label: m.email })),
  ];
  const memberEmail = (userId: string | null | undefined) =>
    userId ? (members.find((m) => m.user_id === userId)?.email ?? userId) : null;

  async function setRepresented(p: Persona, userId: string) {
    try {
      await update.mutateAsync({ name: p.name, body: { represented_user: userId || null } });
      addToast(
        'success',
        userId
          ? `"${p.name}" now represents ${memberEmail(userId)}`
          : `"${p.name}" no longer represents anyone`,
      );
    } catch (exc) {
      addToast('error', `Update failed: ${errorMessage(exc)}`);
    }
  }

  return (
    <section aria-labelledby="org-personas-heading" className={`space-y-3 ${className ?? ''}`}>
      <div className="flex items-center justify-between">
        <div>
          <h2 id="org-personas-heading" className="text-lg font-semibold">
            {isAdmin ? 'Org personas' : 'Your org persona'}
          </h2>
          <p className="text-sm text-text-tertiary">
            {isAdmin
              ? 'Shared by every project in this organization. A project persona with the same name overrides the org one in that project. The member a persona represents can edit it.'
              : 'This org persona represents you. You can edit its role, content and specializations.'}
          </p>
        </div>
        {isAdmin && (
          <Button variant="primary" size="sm" onClick={() => setEdit({ mode: 'create' })}>
            New org persona
          </Button>
        )}
      </div>

      {isLoading && <p>Loading org personas…</p>}
      {error && <p role="alert">Failed to load org personas: {errorMessage(error)}</p>}
      {data && data.length === 0 && (
        <div className="border border-border rounded-lg p-6 text-center text-sm text-text-tertiary">
          No org personas yet.
        </div>
      )}
      {data && data.length > 0 && (
        <Table<Persona>
          data={data}
          rowKey={(p) => p.id}
          columns={[
            {
              key: 'name',
              header: 'Name',
              render: (p) => <span className="font-mono text-text-primary">{p.name}</span>,
            },
            { key: 'role', header: 'Role', render: (p) => <>{p.role}</> },
            ...(isAdmin
              ? [
                  {
                    key: 'represents',
                    header: 'Represents',
                    render: (p: Persona) => (
                      <Select
                        value={p.represented_user_id ?? ''}
                        onValueChange={(v) => void setRepresented(p, v)}
                        options={
                          p.represented_user_id &&
                          !members.some((m) => m.user_id === p.represented_user_id)
                            ? [...memberOptions, { value: p.represented_user_id, label: p.represented_user_id }]
                            : memberOptions
                        }
                        aria-label={`Member represented by ${p.name}`}
                        className="min-w-[12rem]"
                      />
                    ),
                  },
                ]
              : []),
            {
              key: 'updated',
              header: 'Updated',
              render: (p) => (
                <span className="text-text-tertiary">
                  <RelativeDate iso={p.updated_at} />
                </span>
              ),
            },
            {
              key: 'actions',
              header: '',
              width: 'w-0',
              render: (p) => (
                <div className="flex items-center gap-1 justify-end whitespace-nowrap">
                  <Button variant="secondary" size="sm" onClick={() => setEdit({ mode: 'edit', persona: p })}>
                    Edit
                  </Button>
                  {isAdmin && (
                    <Button variant="danger" size="sm" onClick={() => setDeleting(p)}>
                      Delete
                    </Button>
                  )}
                </div>
              ),
            },
          ]}
        />
      )}

      {edit && (
        <PersonaEditModal
          state={edit}
          noun="org persona"
          onClose={() => setEdit(null)}
          onSubmit={async (body) => {
            try {
              if (edit.mode === 'create') {
                await create.mutateAsync(body);
                addToast('success', `Created org persona "${body.name}"`);
              } else if (edit.persona) {
                await update.mutateAsync({
                  name: edit.persona.name,
                  body: {
                    role: body.role,
                    content: body.content,
                    specializations: body.specializations,
                  },
                });
                addToast('success', `Updated "${edit.persona.name}"`);
              }
              setEdit(null);
            } catch (exc) {
              addToast('error', `Save failed: ${errorMessage(exc)}`);
            }
          }}
        />
      )}

      {deleting && (
        <DeleteConfirmModal
          persona={deleting}
          onClose={() => setDeleting(null)}
          onConfirm={async (force) => {
            try {
              await remove.mutateAsync({ name: deleting.name, force });
              addToast('success', `Deleted "${deleting.name}"`);
              setDeleting(null);
            } catch (exc) {
              addToast('error', `Delete failed: ${errorMessage(exc)}`);
            }
          }}
        />
      )}
    </section>
  );
}
