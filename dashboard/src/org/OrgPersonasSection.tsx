/**
 * GH #70 — org-level personas, managed on the Organization page.
 *
 * An org persona is usable from every project in the organization; a
 * project persona with the same name overrides it inside that project.
 * Rendered for org admins and owners only (the server enforces the same
 * rule: writes need admin standing, owners count).
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
import { ApiError, type Persona } from '../api/client';
import { Button, Table } from '../components/ui';
import { DeleteConfirmModal, PersonaEditModal, type EditState } from '../projects/PersonasTab';

interface OrgPersonasSectionProps {
  orgId: string;
}

function errorMessage(exc: unknown): string {
  return exc instanceof ApiError ? `${exc.status}: ${exc.message}` : String(exc);
}

export default function OrgPersonasSection({ orgId }: OrgPersonasSectionProps) {
  const { data, isLoading, error } = useOrgPersonas(orgId);
  const create = useCreateOrgPersona(orgId);
  const update = useUpdateOrgPersona(orgId);
  const remove = useDeleteOrgPersona(orgId);
  const { addToast } = useToast();

  const [edit, setEdit] = useState<EditState | null>(null);
  const [deleting, setDeleting] = useState<Persona | null>(null);

  return (
    <section aria-labelledby="org-personas-heading" className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h2 id="org-personas-heading" className="text-lg font-semibold">
            Org personas
          </h2>
          <p className="text-sm text-text-tertiary">
            Shared by every project in this organization. A project persona with the same
            name overrides the org one in that project.
          </p>
        </div>
        <Button variant="primary" size="sm" onClick={() => setEdit({ mode: 'create' })}>
          New org persona
        </Button>
      </div>

      {isLoading && <p>Loading org personas…</p>}
      {error && <p role="alert">Failed to load org personas: {String(error)}</p>}
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
                  <Button variant="danger" size="sm" onClick={() => setDeleting(p)}>
                    Delete
                  </Button>
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
              addToast('error', errorMessage(exc));
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
              addToast('error', errorMessage(exc));
            }
          }}
        />
      )}
    </section>
  );
}
