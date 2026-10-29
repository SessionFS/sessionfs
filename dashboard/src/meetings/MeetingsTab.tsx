import { useEffect, useState } from 'react';
import type { MeetingDetail, MeetingScope, MeetingSummary } from '../api/client';
import { Badge, Button, Input } from '../components/ui';
import { useMeetings } from '../hooks/useMeetings';
import { useToast } from '../hooks/useToast';
import { errorMessage } from '../utils/errorMessage';
import { meetingDateLabel } from './format';
import { MeetingDetailView } from './MeetingDetailView';
import { UploadMeetingDialog } from './UploadMeetingDialog';

export interface MeetingsTabProps {
  scope: MeetingScope;
  /** Project's org or group's org (group participant matching). */
  orgId?: string | null;
  /** Shown in the upload dialog ("Saved to …"). */
  scopeLabel?: string;
  canEdit: boolean;
  canAdmin: boolean;
  /** The open meeting (URL `meeting=`), or null for the list. */
  meetingId: string | null;
  /** Highlighted lines (URL `lines=`). */
  lines: string | null;
  onOpenMeeting: (meetingId: string | null) => void;
  onLinesChange: (lines: string | null) => void;
  /** Heading level for the list title; the group page nests it in a section. */
  headingId?: string;
}

/**
 * Meetings for a project (Work → Meetings) or a project group. The list and
 * one meeting's detail share this surface; which one shows is in the URL, so
 * a refresh or a shared link lands on the same meeting and lines.
 */
export default function MeetingsTab({
  scope,
  orgId,
  scopeLabel,
  canEdit,
  canAdmin,
  meetingId,
  lines,
  onOpenMeeting,
  onLinesChange,
  headingId = 'meetings-heading',
}: MeetingsTabProps) {
  const [uploading, setUploading] = useState(false);
  // Warnings from the upload that created the open meeting (dup / skipped extraction).
  const [justUploaded, setJustUploaded] = useState<{ id: string; warnings: string[] } | null>(null);
  const { addToast } = useToast();

  function uploaded(meeting: MeetingDetail) {
    setUploading(false);
    setJustUploaded({ id: meeting.id, warnings: meeting.warnings ?? [] });
    const items = meeting.action_items.filter((i) => i.kind === 'action').length;
    addToast(
      'success',
      meeting.extraction_status === 'done'
        ? `Meeting added with ${items} action ${items === 1 ? 'item' : 'items'}`
        : 'Meeting added',
    );
    onOpenMeeting(meeting.id);
  }

  if (meetingId) {
    return (
      <MeetingDetailView
        key={meetingId}
        meetingId={meetingId}
        scope={scope}
        orgId={orgId}
        canEdit={canEdit}
        canAdmin={canAdmin}
        lines={lines}
        onLinesChange={onLinesChange}
        onBack={() => onOpenMeeting(null)}
        uploadWarnings={justUploaded?.id === meetingId ? justUploaded.warnings : undefined}
      />
    );
  }

  return (
    <>
      <MeetingList
        scope={scope}
        canEdit={canEdit}
        onOpen={onOpenMeeting}
        onAdd={() => setUploading(true)}
        headingId={headingId}
      />
      {uploading && (
        <UploadMeetingDialog
          scope={scope}
          scopeLabel={scopeLabel}
          onClose={() => setUploading(false)}
          onUploaded={uploaded}
        />
      )}
    </>
  );
}

function useDebounced(value: string, ms = 250): string {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

function MeetingList({
  scope,
  canEdit,
  onOpen,
  onAdd,
  headingId,
}: {
  scope: MeetingScope;
  canEdit: boolean;
  onOpen: (id: string) => void;
  onAdd: () => void;
  headingId: string;
}) {
  const [query, setQuery] = useState('');
  const q = useDebounced(query.trim());
  const list = useMeetings(scope, q);
  const meetings = list.data?.pages.flatMap((p) => p.meetings) ?? [];
  const searching = q.length > 0;
  const noneAtAll = !list.isLoading && !list.isError && meetings.length === 0 && !searching;

  return (
    <section aria-labelledby={headingId} className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id={headingId} className="flex-1 text-lg font-semibold text-text-primary">
          Meetings
          {!list.isLoading && meetings.length > 0 && (
            <span className="ml-2 text-sm font-normal text-text-tertiary">
              {meetings.length}
              {list.hasNextPage ? '+' : ''}
            </span>
          )}
        </h2>
        {!noneAtAll && (
          <>
            <div className="relative order-last w-full sm:order-none sm:w-64">
              <SearchIcon />
              <Input
                type="search"
                aria-label="Search meetings"
                placeholder="Search titles and transcripts"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                className="!pl-8"
              />
            </div>
            {canEdit && (
              <Button variant="primary" size="sm" onClick={onAdd}>
                Add meeting
              </Button>
            )}
          </>
        )}
      </div>

      {list.isLoading ? (
        <ul className="divide-y divide-[var(--border)] rounded-lg border border-border" aria-busy="true" aria-label="Loading meetings">
          {[0, 1, 2].map((i) => (
            <li key={i} className="px-4 py-3">
              <div className="h-4 w-1/2 rounded bg-bg-elevated animate-pulse" />
              <div className="mt-2 h-3 w-1/3 rounded bg-bg-elevated animate-pulse" />
            </li>
          ))}
        </ul>
      ) : list.isError ? (
        <p role="alert" className="text-sm text-danger">
          Couldn’t load meetings: {errorMessage(list.error)}
        </p>
      ) : noneAtAll ? (
        <MeetingsEmptyState canEdit={canEdit} onAdd={onAdd} group={scope.kind === 'group'} />
      ) : meetings.length === 0 ? (
        <div className="rounded-lg border border-border px-6 py-8 text-center text-sm text-text-tertiary">
          No meetings match “{q}”.
        </div>
      ) : (
        <>
          <ul className="divide-y divide-[var(--border)] overflow-hidden rounded-lg border border-border">
            {meetings.map((m) => (
              <MeetingRow key={m.id} meeting={m} onOpen={onOpen} />
            ))}
          </ul>
          {list.hasNextPage && (
            <div className="flex justify-center">
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void list.fetchNextPage()}
                loading={list.isFetchingNextPage}
                disabled={list.isFetchingNextPage}
              >
                Show more
              </Button>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function MeetingRow({ meeting: m, onOpen }: { meeting: MeetingSummary; onOpen: (id: string) => void }) {
  const pending = m.pending_action_count;
  const decisions = m.decision_count;
  return (
    <li>
      <button
        type="button"
        onClick={() => onOpen(m.id)}
        className="group flex w-full items-center gap-4 px-4 py-3 text-left transition-colors hover:bg-surface cursor-pointer outline-none focus-visible:shadow-[inset_0_0_0_2px_var(--brand-glow)]"
      >
        <MeetingGlyph />
        <span className="min-w-0 flex-1">
          <span className="block font-medium text-text-primary line-clamp-2 [overflow-wrap:anywhere] sm:truncate sm:[display:block]">{m.title}</span>
          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-text-tertiary">
            <span className="sm:hidden">
              <ExtractionBadge status={m.extraction_status} />
            </span>
            <span>{meetingDateLabel(m)}</span>
            <span aria-hidden="true">·</span>
            <span>
              {m.line_count.toLocaleString()} {m.line_count === 1 ? 'line' : 'lines'}
            </span>
            {pending != null && pending > 0 && (
              <>
                <span aria-hidden="true">·</span>
                <span className="text-text-secondary">
                  {pending} open {pending === 1 ? 'action' : 'actions'}
                </span>
              </>
            )}
            {decisions != null && decisions > 0 && (
              <>
                <span aria-hidden="true">·</span>
                <span>
                  {decisions} {decisions === 1 ? 'decision' : 'decisions'}
                </span>
              </>
            )}
          </span>
        </span>
        <span className="hidden sm:inline-flex">
          <ExtractionBadge status={m.extraction_status} />
        </span>
        <svg className="h-4 w-4 shrink-0 text-text-tertiary transition-transform group-hover:translate-x-0.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <polyline points="9 18 15 12 9 6" />
        </svg>
      </button>
    </li>
  );
}

export function ExtractionBadge({ status }: { status: string }) {
  if (status === 'done') return <Badge variant="success" label="Extracted" tint />;
  if (status === 'failed') return <Badge variant="danger" label="Extraction failed" tint />;
  return <Badge label="Not extracted" />;
}

function MeetingsEmptyState({ canEdit, onAdd, group }: { canEdit: boolean; onAdd: () => void; group: boolean }) {
  return (
    <div className="rounded-xl border border-dashed border-border px-6 py-10 text-center">
      <div className="mx-auto mb-3 flex h-10 w-10 items-center justify-center rounded-full bg-bg-elevated text-text-tertiary">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
          <line x1="8" y1="9" x2="16" y2="9" />
          <line x1="8" y1="13" x2="13" y2="13" />
        </svg>
      </div>
      <p className="text-md font-medium text-text-primary">No meetings yet</p>
      <p className="mx-auto mt-1 max-w-md text-sm text-text-tertiary">
        Add a meeting transcript and SessionFS pulls out the action items, decisions and who was there — each one
        linked to the lines it came from. Turn an action item into a ticket in one step.
        {group ? ' Group meetings can file tickets in any of the group’s projects.' : ''}
      </p>
      {canEdit ? (
        <Button variant="primary" size="sm" className="mt-4" onClick={onAdd}>
          Add meeting
        </Button>
      ) : (
        <p className="mt-3 text-xs text-text-tertiary">People who can edit here can add meetings.</p>
      )}
    </div>
  );
}

function MeetingGlyph() {
  return (
    <span aria-hidden="true" className="hidden sm:inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-bg-elevated text-text-tertiary">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
      </svg>
    </span>
  );
}

function SearchIcon() {
  return (
    <svg className="pointer-events-none absolute left-2.5 top-1/2 z-10 h-3.5 w-3.5 -translate-y-1/2 text-text-tertiary" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="7" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  );
}
