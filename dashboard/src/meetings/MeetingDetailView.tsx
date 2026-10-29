import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ApiError, type MeetingActionItem, type MeetingScope } from '../api/client';
import { Alert, Badge, Button, Dialog, DialogFooter, DialogHeader } from '../components/ui';
import RelativeDate from '../components/RelativeDate';
import { useMe } from '../hooks/useMe';
import {
  useDeleteMeeting,
  useDismissMeetingItem,
  useExtractMeeting,
  useMeeting,
  useRestoreMeetingItem,
} from '../hooks/useMeetings';
import { useToast } from '../hooks/useToast';
import {
  AI_KEY_SETTINGS_HREF,
  extractErrorMessage,
  extractionReason,
  linesLabel,
  meetingActionError,
  meetingDateLabel,
  meetingHref,
  meetingScopeHref,
  parseLines,
  parseWarnings,
  type LineRange,
} from './format';
import { ItemList } from './Items';
import { Participants } from './Participants';
import { participantName, useMeetingPeople } from './people';
import { PromoteDialog } from './PromoteDialog';
import { Transcript } from './Transcript';

interface MeetingDetailViewProps {
  meetingId: string;
  scope: MeetingScope;
  /** Org of the project or group (group participant matching uses its members). */
  orgId?: string | null;
  canEdit: boolean;
  /** Project admin / group admin: may delete any meeting here (the uploader always may). */
  canAdmin: boolean;
  /** `lines=12-15` from the URL (an item click or a helper citation). */
  lines: string | null;
  onLinesChange: (lines: string | null) => void;
  onBack: () => void;
  /** Warnings from the upload that created this meeting (shown once). */
  uploadWarnings?: string[];
}

export function MeetingDetailView({
  meetingId,
  scope,
  orgId,
  canEdit,
  canAdmin,
  lines,
  onLinesChange,
  onBack,
  uploadWarnings,
}: MeetingDetailViewProps) {
  const meeting = useMeeting(meetingId);
  const people = useMeetingPeople(scope, orgId);
  const { data: me } = useMe();
  const { addToast } = useToast();
  const extract = useExtractMeeting(meetingId);
  const dismiss = useDismissMeetingItem(meetingId);
  const restore = useRestoreMeetingItem(meetingId);
  const del = useDeleteMeeting(meetingId);

  const [selectedItem, setSelectedItem] = useState<string | null>(null);
  const [scrollKey, setScrollKey] = useState(0);
  const [promoting, setPromoting] = useState<MeetingActionItem | null>(null);
  const [extractError, setExtractError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  // Every item with a dismiss/restore in flight (two can run at once).
  const [busyItems, setBusyItems] = useState<ReadonlySet<string>>(() => new Set());
  const navigate = useNavigate();

  const m = meeting.data;
  // The meeting id comes from the URL and may belong to another project or
  // group. The page's permissions, people and ticket links are only right
  // for its own scope, so a foreign meeting is opened on its own page.
  const inScope =
    !m || (scope.kind === 'project' ? m.project_id === scope.id : m.group_id === scope.id);
  const elsewhere = m && !inScope ? meetingScopeHref(m, lines) : null;
  useEffect(() => {
    if (elsewhere) navigate(elsewhere, { replace: true });
  }, [elsewhere, navigate]);
  const highlight: LineRange | null = useMemo(() => {
    const r = parseLines(lines);
    if (!r || !m) return r;
    // Clamp to the stored text so a stale citation still lands somewhere.
    const end = Math.min(r.end, Math.max(m.line_count, 1));
    return { start: Math.min(r.start, end), end };
  }, [lines, m]);

  const back = (
    <Button variant="ghost" size="sm" onClick={onBack} className="-ml-2">
      <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <polyline points="15 18 9 12 15 6" />
      </svg>
      All meetings
    </Button>
  );

  if (meeting.isLoading) {
    return (
      <div>
        {back}
        <p className="py-10 text-center text-sm text-text-tertiary">Loading meeting…</p>
      </div>
    );
  }

  if (m && !inScope) {
    return (
      <div>
        {back}
        <p className="py-10 text-center text-sm text-text-tertiary">Opening meeting…</p>
      </div>
    );
  }

  if (meeting.isError || !m) {
    const gone = meeting.error instanceof ApiError && meeting.error.status === 404;
    return (
      <div className="space-y-3">
        {back}
        <Alert variant={gone ? 'info' : 'danger'} title={gone ? 'Meeting not found' : 'Couldn’t load this meeting'}>
          {gone
            ? 'It may have been deleted, or you don’t have access to it.'
            : meetingActionError(meeting.error)}
        </Alert>
      </div>
    );
  }

  // A link with lines (an item's own range) selects that item too.
  const selectedId =
    selectedItem ??
    (highlight
      ? m.action_items.find((i) => i.line_start === highlight.start && i.line_end === highlight.end)?.id ?? null
      : null);
  const actions = m.action_items.filter((i) => i.kind === 'action');
  const decisions = m.action_items.filter((i) => i.kind === 'decision');
  const openActions = actions.filter((i) => i.status === 'pending').length;
  const warnings = parseWarnings(uploadWarnings);
  const canDelete = canEdit && (canAdmin || (!!me?.user_id && me.user_id === m.created_by_user_id));
  const status = m.extraction_status;

  function selectItem(item: MeetingActionItem) {
    setSelectedItem(item.id);
    setScrollKey((k) => k + 1);
    onLinesChange(item.line_start === item.line_end ? String(item.line_start) : `${item.line_start}-${item.line_end}`);
  }

  function runExtract() {
    setExtractError(null);
    extract.mutate(undefined, {
      onSuccess: (result) => {
        if (result.extraction_status === 'failed') {
          setExtractError(extractionReason(result.extraction_error));
          return;
        }
        const n = result.action_items.filter((i) => i.kind === 'action').length;
        const dropped = result.dropped_items > 0 ? ` (${result.dropped_items} skipped: no valid line citation)` : '';
        addToast('success', `Found ${n} action ${n === 1 ? 'item' : 'items'}${dropped}`);
      },
      onError: (err) => setExtractError(extractErrorMessage(err)),
    });
  }

  // mutateAsync per call: with mutate(), a second call on the same mutation
  // detaches the first one's callbacks, so its item would stay busy forever.
  async function itemWrite(kind: 'dismiss' | 'restore', item: MeetingActionItem) {
    setBusyItems((prev) => new Set(prev).add(item.id));
    const mutation = kind === 'dismiss' ? dismiss : restore;
    try {
      await mutation.mutateAsync(item.id);
      addToast('success', kind === 'dismiss' ? 'Dismissed' : 'Restored');
    } catch (err) {
      addToast('error', meetingActionError(err));
    } finally {
      setBusyItems((prev) => {
        const next = new Set(prev);
        next.delete(item.id);
        return next;
      });
    }
  }

  const ownerName = (p: Parameters<typeof participantName>[0]) => participantName(p, people);
  const projectId = scope.kind === 'project' ? scope.id : null;
  const keyMissing = extractError === extractionReason('llm_key_required') || warnings.extractionSkipped === 'llm_key_required';

  return (
    <div className="min-w-0">
      {back}

      {/* ── Header ── */}
      <div className="mt-2 flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0 flex-[1_1_20rem]">
          <h2 className="text-lg font-semibold text-text-primary [overflow-wrap:anywhere]">{m.title}</h2>
          <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-text-tertiary">
            <span>{meetingDateLabel(m)}</span>
            {m.duration_minutes ? (
              <>
                <Dot />
                <span>{m.duration_minutes} min</span>
              </>
            ) : null}
            <Dot />
            <span>
              {m.line_count.toLocaleString()} {m.line_count === 1 ? 'line' : 'lines'}
            </span>
            <Dot />
            <span>
              Added <RelativeDate iso={m.created_at} />
            </span>
            {m.dlp_findings > 0 && (
              <>
                <Dot />
                <span title="Removed by your data protection policy before the transcript was stored">
                  {m.dlp_findings} {m.dlp_findings === 1 ? 'item' : 'items'} redacted
                </span>
              </>
            )}
          </p>
        </div>
        {canDelete && (
          <Button variant="ghost" size="sm" onClick={() => setConfirmDelete(true)} className="text-text-tertiary">
            Delete
          </Button>
        )}
      </div>

      {/* ── One-time upload notices ── */}
      {(warnings.duplicateOf || (warnings.extractionSkipped && !extractError)) && (
        <div className="mt-4 space-y-2">
          {warnings.duplicateOf && (
            <Alert variant="info">
              This transcript matches one already here:{' '}
              <Link to={meetingHref(warnings.duplicateOf)} className="text-brand underline-offset-2 hover:underline">
                open the earlier copy
              </Link>
              .
            </Alert>
          )}
          {warnings.extractionSkipped && !extractError && (
            <Alert variant="warning">
              Saved without action items. {extractionReason(warnings.extractionSkipped)}
              {warnings.extractionSkipped === 'llm_key_required' && (
                <>
                  {' '}
                  <KeyLink />
                </>
              )}
            </Alert>
          )}
        </div>
      )}

      <div className="mt-5 grid gap-5 lg:grid-cols-[minmax(0,1fr)_22rem]">
        {/* ── Side panel (first on phones: the items are what people came for) ── */}
        <aside aria-label="Meeting notes" className="min-w-0 space-y-6 lg:order-2">
          <ExtractionStatus
            status={status}
            reason={extractError ?? (status === 'failed' ? extractionReason(m.extraction_error) : null)}
            keyMissing={keyMissing}
            canEdit={canEdit}
            pending={extract.isPending}
            onExtract={runExtract}
            itemCount={m.action_items.length}
          />

          <PanelSection
            title="Action items"
            count={actions.length}
            note={openActions > 0 && openActions !== actions.length ? `${openActions} open` : undefined}
          >
            {actions.length > 0 ? (
              <ItemList
                items={actions}
                participants={m.participants}
                ownerName={ownerName}
                selectedId={selectedId}
                onSelect={selectItem}
                canEdit={canEdit}
                projectId={projectId}
                onPromote={setPromoting}
                onDismiss={(i) => void itemWrite('dismiss', i)}
                onRestore={(i) => void itemWrite('restore', i)}
                busyItemIds={busyItems}
              />
            ) : (
              <EmptyNote>{status === 'done' ? 'No action items in this meeting.' : 'None yet.'}</EmptyNote>
            )}
          </PanelSection>

          <PanelSection title="Decisions" count={decisions.length}>
            {decisions.length > 0 ? (
              <ItemList
                items={decisions}
                participants={m.participants}
                ownerName={ownerName}
                selectedId={selectedId}
                onSelect={selectItem}
                canEdit={canEdit}
                projectId={projectId}
                onDismiss={(i) => void itemWrite('dismiss', i)}
                onRestore={(i) => void itemWrite('restore', i)}
                busyItemIds={busyItems}
              />
            ) : (
              <EmptyNote>{status === 'done' ? 'No decisions recorded.' : 'None yet.'}</EmptyNote>
            )}
          </PanelSection>

          <PanelSection title="Participants" count={m.participants.length}>
            <Participants meetingId={m.id} participants={m.participants} people={people} canEdit={canEdit} />
          </PanelSection>
        </aside>

        {/* ── Transcript ── */}
        <section aria-labelledby="meeting-transcript-heading" className="min-w-0 lg:order-1">
          <div className="overflow-hidden rounded-xl border border-border bg-bg-primary">
            <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2.5">
              <h3 id="meeting-transcript-heading" className="text-sm font-semibold text-text-primary">
                Transcript
              </h3>
              {highlight ? (
                <span className="flex items-center gap-2 text-xs text-text-tertiary">
                  <span className="font-mono text-brand">{linesLabel(highlight)}</span>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedItem(null);
                      onLinesChange(null);
                    }}
                    className="rounded-sm underline-offset-2 hover:text-text-secondary hover:underline outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
                  >
                    Clear
                  </button>
                </span>
              ) : (
                <span className="text-xs text-text-tertiary">Select an item to see where it came from</span>
              )}
            </div>
            <div className="lg:max-h-[calc(100vh-13rem)] lg:overflow-y-auto">
              <Transcript content={m.content} highlight={highlight} scrollKey={scrollKey} />
            </div>
          </div>
        </section>
      </div>

      {promoting && (
        <PromoteDialog
          meetingId={m.id}
          item={promoting}
          onClose={() => setPromoting(null)}
        />
      )}

      <Dialog
        open={confirmDelete}
        // While the delete runs, Escape and the backdrop don't close it.
        onClose={() => {
          if (!del.isPending) setConfirmDelete(false);
        }}
        titleId="meeting-delete-title"
      >
        <DialogHeader titleId="meeting-delete-title">Delete this meeting?</DialogHeader>
        <p className="mb-4 text-sm text-text-tertiary">
          <span className="font-medium text-text-secondary">{m.title}</span>, its action items and participants are
          removed. Tickets already created from it stay.
        </p>
        <DialogFooter>
          <Button variant="secondary" onClick={() => setConfirmDelete(false)} disabled={del.isPending}>
            Cancel
          </Button>
          <Button
            variant="danger"
            loading={del.isPending}
            disabled={del.isPending}
            onClick={() =>
              del.mutate(undefined, {
                onSuccess: () => {
                  addToast('success', 'Meeting deleted');
                  setConfirmDelete(false);
                  onBack();
                },
                onError: (err) => addToast('error', meetingActionError(err)),
              })
            }
          >
            Delete
          </Button>
        </DialogFooter>
      </Dialog>
    </div>
  );
}

function Dot() {
  return <span aria-hidden="true">·</span>;
}

function EmptyNote({ children }: { children: ReactNode }) {
  return <p className="text-sm text-text-tertiary">{children}</p>;
}

function KeyLink() {
  return (
    <Link to={AI_KEY_SETTINGS_HREF} className="font-medium text-brand underline-offset-2 hover:underline">
      Set up an AI key
    </Link>
  );
}

function PanelSection({
  title,
  count,
  note,
  children,
}: {
  title: string;
  count: number;
  note?: string;
  children: ReactNode;
}) {
  const id = `meeting-panel-${title.toLowerCase().replace(/\s+/g, '-')}`;
  return (
    <section aria-labelledby={id}>
      <h3 id={id} className="mb-2 flex items-baseline gap-2 text-sm font-semibold text-text-primary">
        {title}
        <span className="text-xs font-normal tabular-nums text-text-tertiary">{count}</span>
        {note && <span className="text-xs font-normal text-text-tertiary">· {note}</span>}
      </h3>
      {children}
    </section>
  );
}

function ExtractionStatus({
  status,
  reason,
  keyMissing,
  canEdit,
  pending,
  onExtract,
  itemCount,
}: {
  status: string;
  reason: string | null;
  keyMissing: boolean;
  canEdit: boolean;
  pending: boolean;
  onExtract: () => void;
  itemCount: number;
}) {
  const failed = !!reason;
  const label = pending
    ? 'Extracting…'
    : failed
      ? 'Extraction failed'
      : status === 'done'
        ? 'Extracted'
        : 'Not extracted yet';
  const variant = failed ? 'danger' : status === 'done' && !pending ? 'success' : 'default';
  return (
    <div className="rounded-xl border border-border bg-bg-primary px-3.5 py-3">
      <div className="flex items-center justify-between gap-3">
        <Badge variant={variant} label={label} tint />
        {canEdit && (
          <Button
            variant={status === 'none' && !failed ? 'primary' : 'secondary'}
            size="sm"
            onClick={onExtract}
            loading={pending}
            disabled={pending}
          >
            {status === 'none' && !failed ? 'Extract action items' : 'Re-extract'}
          </Button>
        )}
      </div>
      <p className="mt-2 text-xs text-text-tertiary" role={failed ? 'alert' : undefined}>
        {pending
          ? 'Reading the transcript. This can take up to two minutes.'
          : failed
            ? (
              <>
                {reason}
                {keyMissing && (
                  <>
                    {' '}
                    <KeyLink />
                  </>
                )}
              </>
            )
            : status === 'done'
              ? itemCount > 0 && canEdit
                ? 'Re-extracting replaces open items. Tickets, dismissed items and hand-made matches stay.'
                : 'Every item links to the lines it came from.'
              : canEdit
                ? 'Pull out action items, decisions and who was there, using your AI key or your organization’s.'
                : 'No one has extracted action items from this meeting yet.'}
      </p>
    </div>
  );
}
