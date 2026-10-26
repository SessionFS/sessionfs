import { useState } from 'react';
import { useKnowledgeEntries, useDismissEntry, useCompileProject, useProjectHealth, useDismissStaleEntries, usePromoteEntry, useSupersedeEntry, useRebuildProject, useRefreshEntry } from '../../hooks/useProjects';
import { useToast } from '../../hooks/useToast';
import RelativeDate from '../../components/RelativeDate';
import type { KnowledgeEntry } from '../../api/client';
import { Button, Select } from '../../components/ui';
import { EntryTypeBadge, ClaimClassBadge, FreshnessDot } from './badges';
import { SupersedeDialog } from './SupersedeDialog';
import { errorMessage } from '../../utils/errorMessage';

/**
 * `canEdit` is the project's `can_edit` flag: readers who can only view the
 * project (a captured session on its repo) don't get curation controls —
 * the server refuses those changes with 403 `project_edit_forbidden`.
 */
export default function KnowledgeEntriesTab({
  projectId,
  canEdit = true,
}: {
  projectId: string;
  canEdit?: boolean;
}) {
  const [pendingOnly, setPendingOnly] = useState(false);
  const [typeFilter, setTypeFilter] = useState<string>('');
  const [claimFilter, setClaimFilter] = useState<string>('');
  const [freshnessFilter, setFreshnessFilter] = useState<string>('');
  const [supersedeTarget, setSupersedeTarget] = useState<number | null>(null);
  const [staleQueueOpen, setStaleQueueOpen] = useState(true);
  const { data, isLoading } = useKnowledgeEntries(projectId, {
    pending: pendingOnly || undefined,
    type: typeFilter || undefined,
    limit: 50,
  });
  const dismissEntry = useDismissEntry(projectId);
  const compile = useCompileProject(projectId);
  const rebuild = useRebuildProject(projectId);
  const promote = usePromoteEntry(projectId);
  const supersede = useSupersedeEntry(projectId);
  const refreshEntry = useRefreshEntry(projectId);
  const { data: health } = useProjectHealth(projectId);
  const dismissStale = useDismissStaleEntries(projectId);
  const { addToast } = useToast();

  function handleCompile() {
    compile.mutate(undefined, {
      onSuccess: (result) => {
        const n = result.entries_compiled;
        if (n === 0) {
          addToast('info', 'No pending entries to compile.');
        } else {
          addToast('success', `Project context updated. ${n} entries compiled.`);
        }
      },
      onError: (err) => addToast('error', `Compile failed: ${errorMessage(err)}`),
    });
  }

  function handleRebuild() {
    rebuild.mutate(undefined, {
      onSuccess: () => addToast('success', 'Full rebuild completed.'),
      onError: (err) => addToast('error', `Rebuild failed: ${errorMessage(err)}`),
    });
  }

  function handleDismiss(entry: KnowledgeEntry) {
    dismissEntry.mutate(entry.id, {
      onSuccess: () => addToast('success', `Entry ${entry.id} dismissed.`),
      onError: (err) => addToast('error', `Dismiss failed: ${errorMessage(err)}`),
    });
  }

  function handlePromote(entry: KnowledgeEntry) {
    promote.mutate(entry.id, {
      onSuccess: () => addToast('success', `Entry ${entry.id} promoted to claim.`),
      onError: (err) => addToast('error', `Promote failed: ${errorMessage(err)}`),
    });
  }

  function handleSupersede(entryId: number, supersedingId: number, reason: string) {
    supersede.mutate(
      { entryId, superseding_id: supersedingId, reason },
      {
        onSuccess: () => {
          addToast('success', `Entry ${entryId} marked as superseded.`);
          setSupersedeTarget(null);
        },
        onError: (err) => addToast('error', `Supersede failed: ${errorMessage(err)}`),
      },
    );
  }

  function handleStillValid(entry: KnowledgeEntry) {
    refreshEntry.mutate(entry.id, {
      onSuccess: () => addToast('success', `Entry ${entry.id} marked as still valid.`),
      onError: (err) => addToast('error', `Update failed: ${errorMessage(err)}`),
    });
  }

  const entryTypes = ['decision', 'pattern', 'discovery', 'convention', 'bug', 'dependency'];

  // Filter entries by claim_class and freshness_class client-side
  const filteredEntries = (data?.entries || []).filter((entry) => {
    if (claimFilter && entry.claim_class !== claimFilter) return false;
    if (freshnessFilter && entry.freshness_class !== freshnessFilter) return false;
    return true;
  });

  // Stale claims for review queue
  const staleClaims = (data?.entries || []).filter(
    (e) => e.freshness_class === 'stale' && e.claim_class === 'claim' && !e.dismissed,
  );

  // Compute health distribution from entries
  const allEntries = data?.entries || [];
  const claimCount = allEntries.filter((e) => e.claim_class === 'claim').length;
  const noteCount = allEntries.filter((e) => e.claim_class === 'note').length;
  const evidenceCount = allEntries.filter((e) => e.claim_class === 'evidence').length;
  const currentCount = allEntries.filter((e) => e.freshness_class === 'current').length;
  const agingCount = allEntries.filter((e) => e.freshness_class === 'aging').length;
  const staleCount = allEntries.filter((e) => e.freshness_class === 'stale').length;
  const supersededCount = allEntries.filter((e) => e.freshness_class === 'superseded').length;
  const activeClaimCount = allEntries.filter(
    (e) => e.claim_class === 'claim' && (e.freshness_class === 'current' || e.freshness_class === 'aging'),
  ).length;

  return (
    <div className="p-5">
      {/* Health banner */}
      {health && (
        <div
          className="mb-4 rounded-lg border p-4"
          style={{
            backgroundColor: (health.stale_entry_count > 10) ? 'color-mix(in srgb, var(--danger) 6%, transparent)' : 'color-mix(in srgb, var(--warning) 6%, transparent)',
            borderColor: (health.stale_entry_count > 10) ? 'color-mix(in srgb, var(--danger) 20%, transparent)' : 'color-mix(in srgb, var(--warning) 20%, transparent)',
          }}
        >
          {/* Summary counters */}
          <div className="flex flex-wrap items-center gap-4 text-xs mb-2">
            <span className="text-text-secondary">
              {health.total_entries} total
            </span>
            <span className="text-brand">
              {claimCount} claims
            </span>
            <span className="text-text-tertiary">
              {noteCount} notes
            </span>
            <span className="text-info">
              {evidenceCount} evidence
            </span>
            <span className="text-text-secondary">
              {activeClaimCount} active claims
            </span>
          </div>

          {/* Freshness distribution */}
          <div className="flex flex-wrap items-center gap-3 text-xs mb-2">
            <span className="flex items-center gap-1">
              <FreshnessDot freshnessClass="current" decorative />
              <span className="text-text-tertiary">{currentCount} current</span>
            </span>
            <span className="flex items-center gap-1">
              <FreshnessDot freshnessClass="aging" decorative />
              <span className="text-text-tertiary">{agingCount} aging</span>
            </span>
            <span className="flex items-center gap-1">
              <FreshnessDot freshnessClass="stale" decorative />
              <span className="text-text-tertiary">{staleCount} stale</span>
            </span>
            <span className="flex items-center gap-1">
              <FreshnessDot freshnessClass="superseded" decorative />
              <span className="text-text-tertiary">{supersededCount} superseded</span>
            </span>
          </div>

          {/* Stale/low-confidence/decayed warnings */}
          {(health.stale_entry_count > 0 || health.low_confidence_count > 0 || health.decayed_count > 0) && (
            <div className="flex flex-wrap items-center gap-4 text-sm mb-2">
              {health.stale_entry_count > 0 && (
                <span className="text-warning">
                  {health.stale_entry_count} stale {health.stale_entry_count === 1 ? 'entry' : 'entries'}
                </span>
              )}
              {health.low_confidence_count > 0 && (
                <span className="text-text-tertiary">
                  {health.low_confidence_count} low-confidence
                </span>
              )}
              {health.decayed_count > 0 && (
                <span className="text-text-tertiary">
                  {health.decayed_count} decayed
                </span>
              )}
            </div>
          )}

          {health.recommendations.length > 0 && (
            <ul className="space-y-1">
              {health.recommendations.map((r, i) => (
                <li key={i} className="text-xs text-text-secondary">
                  {r}
                </li>
              ))}
            </ul>
          )}
          {canEdit && health.stale_entry_count > 0 && (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => dismissStale.mutate(undefined, {
                onSuccess: (result) => addToast('success', `Dismissed ${result.dismissed_count} stale entries.`),
                onError: (err) => addToast('error', `Dismiss failed: ${errorMessage(err)}`),
              })}
              disabled={dismissStale.isPending}
              loading={dismissStale.isPending}
              className="mt-2"
            >
              Dismiss {health.stale_entry_count} stale
            </Button>
          )}
        </div>
      )}

      {/* Stale review queue */}
      {staleClaims.length > 0 && (
        <div className="mb-4 rounded-lg border border-border bg-bg-primary">
          <button
            type="button"
            onClick={() => setStaleQueueOpen(!staleQueueOpen)}
            aria-expanded={staleQueueOpen}
            className={`w-full flex items-center justify-between px-4 py-2.5 text-sm font-medium text-text-secondary hover:bg-surface-hover transition-colors outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]`}
          >
            <span>Stale Claims Review ({staleClaims.length})</span>
            <span className="text-2xs">{staleQueueOpen ? '\u25B2' : '\u25BC'}</span>
          </button>
          {staleQueueOpen && (
            <div className="border-t border-border px-4 py-3 space-y-2">
              {staleClaims.map((entry) => (
                <div
                  key={`stale-${entry.id}`}
                  className="flex flex-wrap items-start gap-3 px-3 py-2 rounded border border-border bg-bg-elevated"
                >
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-text-primary">{entry.content}</p>
                    <p className="text-xs text-text-tertiary mt-0.5">
                      #{entry.id} &middot; {entry.entry_type}
                    </p>
                  </div>
                  {canEdit && (
                  <div className="flex items-center gap-1 shrink-0">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleStillValid(entry)}
                      disabled={refreshEntry.isPending}
                      className="font-normal text-brand hover:text-brand"
                    >
                      Still valid
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setSupersedeTarget(entry.id)}
                      className="font-normal text-text-tertiary"
                    >
                      Supersede
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleDismiss(entry)}
                      disabled={dismissEntry.isPending}
                      className="font-normal text-text-tertiary hover:text-danger"
                    >
                      Dismiss
                    </Button>
                  </div>
                  )}
                  {supersedeTarget === entry.id && (
                    <SupersedeDialog
                      entryId={entry.id}
                      onConfirm={(sid, reason) => handleSupersede(entry.id, sid, reason)}
                      onCancel={() => setSupersedeTarget(null)}
                      isPending={supersede.isPending}
                    />
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Toolbar */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        <div className="flex items-center gap-3 flex-wrap">
          <label className="flex items-center gap-1.5 text-sm text-text-secondary">
            <input
              type="checkbox"
              checked={pendingOnly}
              onChange={(e) => setPendingOnly(e.target.checked)}
              className="accent-[var(--brand)]"
            />
            Pending only
          </label>
          <Select
            aria-label="Filter by entry type"
            value={typeFilter}
            onValueChange={setTypeFilter}
            options={[
              { value: '', label: 'All types' },
              ...entryTypes.map((t) => ({ value: t, label: t })),
            ]}
            className="!w-auto !px-2 !py-1 bg-bg-primary text-sm"
          />
          <Select
            aria-label="Filter by claim class"
            value={claimFilter}
            onValueChange={setClaimFilter}
            options={[
              { value: '', label: 'All classes' },
              { value: 'claim', label: 'Claim' },
              { value: 'note', label: 'Note' },
              { value: 'evidence', label: 'Evidence' },
            ]}
            className="!w-auto !px-2 !py-1 bg-bg-primary text-sm"
          />
          <Select
            aria-label="Filter by freshness"
            value={freshnessFilter}
            onValueChange={setFreshnessFilter}
            options={[
              { value: '', label: 'All freshness' },
              { value: 'current', label: 'Current' },
              { value: 'aging', label: 'Aging' },
              { value: 'stale', label: 'Stale' },
            ]}
            className="!w-auto !px-2 !py-1 bg-bg-primary text-sm"
          />
        </div>
        {canEdit && (
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            onClick={handleRebuild}
            disabled={rebuild.isPending}
            loading={rebuild.isPending}
          >
            Rebuild
          </Button>
          <Button
            onClick={handleCompile}
            disabled={compile.isPending}
            loading={compile.isPending}
          >
            Compile now
          </Button>
        </div>
        )}
      </div>

      {/* Entries list */}
      {isLoading ? (
        <p className="text-text-tertiary text-sm py-4">Loading entries…</p>
      ) : !filteredEntries.length ? (
        <p className="text-text-tertiary text-sm py-8 text-center">
          No knowledge entries found. Entries are auto-extracted from sessions.
        </p>
      ) : (
        <div className="space-y-2">
          {filteredEntries.map((entry) => (
            <div
              key={entry.id}
              className={`px-4 py-3 rounded-lg border transition-colors ${
                entry.freshness_class === 'superseded'
                  ? 'border-border bg-bg-primary opacity-50'
                  : entry.dismissed
                    ? 'border-border bg-bg-primary opacity-60'
                    : 'border-border bg-bg-elevated'
              }`}
            >
              <div className="flex flex-wrap items-start gap-3">
                <div className="shrink-0 pt-0.5 flex items-center gap-1.5">
                  <FreshnessDot freshnessClass={entry.freshness_class} />
                  <EntryTypeBadge type={entry.entry_type} />
                  <ClaimClassBadge claimClass={entry.claim_class} />
                </div>
                <div className="flex-1 min-w-[10rem]">
                  <p
                    className={`text-sm text-text-primary ${
                      entry.dismissed || entry.freshness_class === 'superseded'
                        ? 'line-through text-text-tertiary'
                        : ''
                    }`}
                  >
                    {entry.content}
                  </p>
                  {/* Provenance block */}
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-text-tertiary">
                    <span>
                      <RelativeDate iso={entry.created_at} />
                    </span>
                    {entry.session_id && (
                      <>
                        <span>&middot;</span>
                        <span className="font-mono">
                          {entry.session_id.length > 14
                            ? entry.session_id.slice(0, 14) + '..'
                            : entry.session_id}
                        </span>
                      </>
                    )}
                    {entry.entity_ref && (
                      <>
                        <span>&middot;</span>
                        <span className="font-mono">{entry.entity_type}: {entry.entity_ref}</span>
                      </>
                    )}
                    {entry.compiled_at && (
                      <>
                        <span>&middot;</span>
                        <span className="text-success">compiled</span>
                      </>
                    )}
                    {entry.confidence !== undefined && entry.confidence < 1 && (
                      <>
                        <span>&middot;</span>
                        <span>{Math.round(entry.confidence * 100)}% confidence</span>
                      </>
                    )}
                    <span>&middot;</span>
                    <span>
                      retrieved: {entry.retrieved_count} / used: {entry.used_in_answer_count} / compiled: {entry.compiled_count}
                    </span>
                    {entry.superseded_by && (
                      <>
                        <span>&middot;</span>
                        <span className="text-danger">superseded by #{entry.superseded_by}</span>
                      </>
                    )}
                    {entry.supersession_reason && (
                      <>
                        <span>&middot;</span>
                        <span className="text-text-tertiary">{entry.supersession_reason}</span>
                      </>
                    )}
                  </div>
                </div>
                {/* Actions (curation is editor-only) */}
                {canEdit && (
                <div className="flex items-center gap-1 shrink-0">
                  {!entry.dismissed && entry.claim_class === 'note' && entry.freshness_class !== 'superseded' && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handlePromote(entry)}
                      disabled={promote.isPending}
                      className="font-normal text-brand hover:text-brand"
                      title="Promote to Claim"
                    >
                      Promote
                    </Button>
                  )}
                  {!entry.dismissed && entry.freshness_class !== 'superseded' && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setSupersedeTarget(supersedeTarget === entry.id ? null : entry.id)}
                      className="font-normal text-text-tertiary"
                      title="Mark Superseded"
                    >
                      Supersede
                    </Button>
                  )}
                  {!entry.dismissed && !entry.compiled_at && entry.freshness_class !== 'superseded' && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => handleDismiss(entry)}
                      disabled={dismissEntry.isPending}
                      className="shrink-0 font-normal text-text-tertiary hover:text-danger"
                      title="Dismiss this entry"
                    >
                      Dismiss
                    </Button>
                  )}
                </div>
                )}
              </div>
              {/* Supersede dialog inline */}
              {supersedeTarget === entry.id && (
                <SupersedeDialog
                  entryId={entry.id}
                  onConfirm={(sid, reason) => handleSupersede(entry.id, sid, reason)}
                  onCancel={() => setSupersedeTarget(null)}
                  isPending={supersede.isPending}
                />
              )}
            </div>
          ))}
          <p className="text-xs text-text-tertiary pt-2">
            {data && data.total > filteredEntries.length
              ? `Showing ${filteredEntries.length} of ${data.total} entries`
              : `${filteredEntries.length} ${filteredEntries.length === 1 ? 'entry' : 'entries'}`}
          </p>
        </div>
      )}
    </div>
  );
}
