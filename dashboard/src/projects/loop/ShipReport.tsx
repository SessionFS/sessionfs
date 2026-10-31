import { useMemo, useState } from 'react';
import { Alert, Badge, Button, Card, Select } from '../../components/ui';
import type { Harm, ShipReport as ShipReportData } from '../../api/reviewLoop';
import { useReleases, useShipReport } from '../../hooks/useReviewLoop';
import { errorMessage } from '../../utils/errorMessage';
import { safeHttpUrl } from '../../utils/url';
import { ShareLinkDialog } from './ShareLinkDialog';
import { HARM_LABEL, formatTokens, formatUsd, isHarmful, plural } from './format';

const SECTION_TITLE = 'text-md font-semibold text-text-primary';
const QUIET = 'text-sm text-text-tertiary';

/** `YYYY-MM` of the current month (UTC, matching the server's month window). */
export function currentMonth(now: Date = new Date()): string {
  return now.toISOString().slice(0, 7);
}

/** The factual headline, e.g. "v0.29.0 — 4 loops, 14 review rounds, 63 findings, 19 harmful, all fixed before merge". */
export function shipHeadline(r: ShipReportData): string {
  const parts = [
    plural(r.loops, 'loop'),
    plural(r.review_rounds, 'review round'),
    plural(r.findings, 'finding'),
    `${r.harmful} harmful`,
    r.harmful === 0 ? 'nothing harmful found' : r.all_fixed_before_merge ? 'all fixed before merge' : 'some still open',
  ];
  return `${r.version} — ${parts.join(', ')}`;
}

function costLine(r: ShipReportData): string {
  const c = r.cost_per_accepted_change;
  return c.cost_usd !== undefined ? `${formatUsd(c.cost_usd)} (${formatTokens(c.tokens)} tokens)` : `${formatTokens(c.tokens)} tokens`;
}

/** Plain-text lines drawn on the exported image (mirrors the card, no HTML involved). */
function imageLines(r: ShipReportData): string[] {
  const lines = [
    `Harmful findings: ${r.harmful}` +
      (r.harmful > 0
        ? ` (${Object.entries(r.harmful_by_harm)
            .map(([h, n]) => `${n} ${(HARM_LABEL[h as Harm] ?? h).toLowerCase()}`)
            .join(', ')})`
        : ''),
    `Cost per accepted change: ${costLine(r)}`,
  ];
  if (r.checklist_classes_learned.length > 0) {
    lines.push(`Checklist classes learned: ${r.checklist_classes_learned.map((c) => c.title || c.class).join(', ')}`);
  }
  if (r.gates.length > 0) {
    const n = (s: string) => r.gates.filter((g) => g.status === s).length;
    lines.push(`Gates: ${n('passed')} passed, ${n('failed')} failed, ${n('skipped')} skipped`);
  }
  if (r.deployments.length > 0) {
    lines.push(`Deployed: ${[...new Set(r.deployments.map((d) => d.environment))].join(', ')}`);
  }
  return lines;
}

function wrap(ctx: CanvasRenderingContext2D, text: string, maxWidth: number): string[] {
  const out: string[] = [];
  let line = '';
  for (const word of text.split(' ')) {
    const next = line ? `${line} ${word}` : word;
    if (line && ctx.measureText(next).width > maxWidth) {
      out.push(line);
      line = word;
    } else {
      line = next;
    }
  }
  if (line) out.push(line);
  return out;
}

/** Render the card to a PNG with canvas text (no DOM cloning, no new dependency). Resolves null when canvas is unavailable. */
export function renderShipImage(r: ShipReportData): Promise<Blob | null> {
  const canvas = document.createElement('canvas');
  const ctx = canvas.getContext('2d');
  if (!ctx) return Promise.resolve(null);
  const css = getComputedStyle(document.documentElement);
  const tok = (name: string, fallback: string) => css.getPropertyValue(name).trim() || fallback;
  const width = 1200;
  const pad = 64;
  const scale = 2;
  const font = css.fontFamily || 'sans-serif';

  ctx.font = `600 40px ${font}`;
  const head = wrap(ctx, shipHeadline(r), width - pad * 2);
  const body = imageLines(r);
  const height = pad * 2 + 30 + head.length * 54 + 24 + body.length * 44 + 56;
  canvas.width = width * scale;
  canvas.height = height * scale;
  ctx.scale(scale, scale);
  ctx.fillStyle = tok('--bg-primary', 'white');
  ctx.fillRect(0, 0, width, height);
  ctx.textBaseline = 'top';
  let y = pad;
  ctx.fillStyle = tok('--text-tertiary', 'gray');
  ctx.font = `500 22px ${font}`;
  ctx.fillText('SHIP REPORT', pad, y);
  y += 30 + 12;
  ctx.fillStyle = tok('--text-primary', 'black');
  ctx.font = `600 40px ${font}`;
  for (const l of head) {
    ctx.fillText(l, pad, y);
    y += 54;
  }
  y += 24;
  ctx.font = `400 26px ${font}`;
  for (const l of body) {
    ctx.fillStyle = l.startsWith('Harmful') && r.harmful > 0 ? tok('--danger', 'red') : tok('--text-secondary', 'gray');
    ctx.fillText(l, pad, y);
    y += 44;
  }
  ctx.fillStyle = tok('--text-tertiary', 'gray');
  ctx.font = `400 20px ${font}`;
  ctx.fillText('Counts of stored records. Recorded by SessionFS.', pad, height - pad + 8);
  return new Promise((resolve) => canvas.toBlob((b) => resolve(b), 'image/png'));
}

function Stat({ label, value, loud = false }: { label: string; value: number | string; loud?: boolean }) {
  return (
    <div>
      <dt className="text-2xs uppercase tracking-[0.04em] text-text-tertiary">{label}</dt>
      <dd className={`text-lg font-semibold ${loud ? 'text-danger' : 'text-text-primary'}`}>{value}</dd>
    </div>
  );
}

export function ShipReportCard({ report }: { report: ShipReportData }) {
  const harms = Object.entries(report.harmful_by_harm).filter(([, n]) => (n ?? 0) > 0);
  const count = (s: string) => report.gates.filter((g) => g.status === s).length;
  return (
    <Card className="space-y-5 p-5" data-testid="ship-report-card">
      <h3 className="text-lg font-semibold text-text-primary" data-testid="ship-headline">
        {shipHeadline(report)}
      </h3>
      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-5">
        <Stat label="Loops" value={report.loops} />
        <Stat label="Review rounds" value={report.review_rounds} />
        <Stat label="Findings" value={report.findings} />
        <Stat label="Harmful" value={report.harmful} loud={report.harmful > 0} />
        <Stat label="Cost per accepted change" value={costLine(report)} />
      </dl>
      <div className="space-y-1">
        <h4 className={SECTION_TITLE}>Harm caught</h4>
        {harms.length === 0 ? (
          <p className={QUIET}>No harmful findings were recorded for this report.</p>
        ) : (
          <ul className="flex flex-wrap gap-2" aria-label="Harmful findings by harm">
            {harms.map(([h, n]) => (
              <li key={h}>
                {isHarmful(h) ? (
                  <Badge variant="danger" tint label={`${HARM_LABEL[h as Harm] ?? h}: ${n}`} />
                ) : (
                  <Badge label={`${HARM_LABEL[h as Harm] ?? h}: ${n}`} />
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="space-y-1">
        <h4 className={SECTION_TITLE}>Checklist classes learned</h4>
        {report.checklist_classes_learned.length === 0 ? (
          <p className={QUIET}>None joined the checklist in this window.</p>
        ) : (
          <ul className="list-disc pl-5 text-sm text-text-secondary">
            {report.checklist_classes_learned.map((c) => (
              <li key={c.class}>{c.title || c.class}</li>
            ))}
          </ul>
        )}
      </div>
      <div className="grid gap-5 sm:grid-cols-2">
        <div className="space-y-1">
          <h4 className={SECTION_TITLE}>Gates</h4>
          {report.gates.length === 0 ? (
            <p className={QUIET}>No gates recorded.</p>
          ) : (
            <>
              <p className="text-sm text-text-secondary" data-testid="ship-gates">
                {count('passed')} passed, {count('failed')} failed, {count('skipped')} skipped
              </p>
              <ul className="text-xs text-text-tertiary">
                {report.gates.map((g, i) => (
                  <li key={`${g.kind}-${i}`}>
                    {g.kind}: {g.status}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
        <div className="space-y-1">
          <h4 className={SECTION_TITLE}>Deployments</h4>
          {report.deployments.length === 0 ? (
            <p className={QUIET}>No deployments recorded.</p>
          ) : (
            <ul className="text-sm text-text-secondary">
              {report.deployments.map((d, i) => (
                <li key={`${d.environment}-${i}`}>
                  {d.environment}
                  {d.source ? <span className="text-text-tertiary"> via {d.source}</span> : null}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      <div className="space-y-1">
        <h4 className={SECTION_TITLE}>Pull requests</h4>
        {report.prs.length === 0 ? (
          <p className={QUIET}>No pull requests linked.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {report.prs.map((p) => {
              const href = safeHttpUrl(p.external_url);
              const label = `${p.repo ?? p.provider}${p.number ? ` #${p.number}` : ''}${p.title ? `: ${p.title}` : ''}`;
              return (
                <li key={p.id}>
                  {href ? (
                    <a className="text-brand underline" href={href} target="_blank" rel="noopener noreferrer">
                      {label}
                      <span className="sr-only"> (opens in a new tab)</span>
                    </a>
                  ) : (
                    <span className="text-text-secondary">{label}</span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Card>
  );
}

/** Ship Reports view: release (or month) picker, the story card, and two exports. */
export function ShipReports({ projectId, canEdit = false }: { projectId: string; canEdit?: boolean }) {
  const [sharing, setSharing] = useState(false);
  const releases = useReleases(projectId);
  const [picked, setPicked] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const list = releases.data?.releases ?? [];
  const thisMonth = `month:${currentMonth()}`;
  const selection = picked ?? (list.length > 0 ? list[0].version : null);
  const report = useShipReport(projectId, selection);

  const options = useMemo(
    () => [
      { value: thisMonth, label: 'This month' },
      ...list.map((r) => ({ value: r.version, label: r.version })),
    ],
    [list, thisMonth],
  );

  if (releases.isLoading) return <p className="p-4 text-text-tertiary">Loading releases…</p>;
  if (releases.error) return <Alert variant="danger">Could not load releases: {errorMessage(releases.error)}</Alert>;
  if (list.length === 0) {
    return (
      <Card className="p-8 text-center" data-testid="ship-empty">
        <p className="mb-1 text-md font-semibold text-text-primary">No releases recorded yet</p>
        <p className="mx-auto max-w-xl text-sm text-text-tertiary">
          A Ship Report tells the story of one release: how many loops and review rounds went into it, what was caught, what it cost and where it was deployed.
        </p>
        <p className="mt-4 text-sm text-text-secondary">
          Record your first release with <code className="font-mono text-text-primary">sfs release record</code>.
        </p>
      </Card>
    );
  }

  async function copyMarkdown() {
    if (!report.data) return;
    try {
      await navigator.clipboard.writeText(report.data.markdown);
      setNote('Markdown copied.');
    } catch {
      setNote('Could not copy. Your browser blocked clipboard access.');
    }
  }

  async function downloadImage() {
    if (!report.data) return;
    const blob = await renderShipImage(report.data);
    if (!blob) {
      setNote('Image export is not available in this browser. Copy the markdown instead.');
      return;
    }
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `ship-report-${report.data.version}.png`;
    a.click();
    URL.revokeObjectURL(url);
    setNote('Image downloaded.');
  }

  return (
    <section aria-labelledby="ship-heading" className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h3 id="ship-heading" className={SECTION_TITLE}>Ship Reports</h3>
          <p className={QUIET}>One release, or the whole month, as a factual story.</p>
        </div>
        <Select
          aria-label="Release"
          value={selection ?? ''}
          onValueChange={(v) => {
            setPicked(v);
            setNote(null);
          }}
          options={options}
          className="w-48"
        />
      </div>
      {report.isLoading && <p className="text-text-tertiary">Loading the report…</p>}
      {report.error && <Alert variant="danger">Could not load this report: {errorMessage(report.error)}</Alert>}
      {report.data && (
        <>
          <ShipReportCard report={report.data} />
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="secondary" size="sm" onClick={() => void copyMarkdown()}>Copy markdown</Button>
            <Button variant="secondary" size="sm" onClick={() => void downloadImage()}>Download image</Button>
            {canEdit && <Button variant="secondary" size="sm" onClick={() => setSharing(true)}>Share</Button>}
            <span role="status" className="text-xs text-text-tertiary">{note}</span>
          </div>
        </>
      )}
      {sharing && selection && (
        <ShareLinkDialog
          projectId={projectId}
          target={
            selection.startsWith('month:')
              ? { kind: 'ship_report', month: selection.slice(6) }
              : { kind: 'ship_report', releaseVersion: selection }
          }
          onClose={() => setSharing(false)}
        />
      )}
    </section>
  );
}
