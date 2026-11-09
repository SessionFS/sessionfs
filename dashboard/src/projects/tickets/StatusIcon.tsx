/* ── Status and priority icons ──
 *
 * Status is told by shape AND colour, never colour alone: a dashed ring
 * (suggested), a ring (open), a half-filled ring (in progress), a
 * three-quarter ring (in review), a struck ring (blocked), a filled check
 * (done / closed) and a filled cross (cancelled). Colours come from
 * tickets.css (`tk-st-<status>`). Both icons are decorative; callers put the
 * words in an sr-only span or the control's label. */

export function StatusIcon({ status, size = 16 }: { status: string; size?: number }) {
  const r = 6.25;
  const ring = <circle cx="8" cy="8" r={r} fill="none" stroke="currentColor" strokeWidth="1.6" />;
  let body;
  switch (status) {
    case 'suggested':
      body = <circle cx="8" cy="8" r={r} fill="none" stroke="currentColor" strokeWidth="1.6" strokeDasharray="2.4 2.2" />;
      break;
    case 'open':
      body = ring;
      break;
    case 'in_progress':
      body = (
        <>
          {ring}
          <path d="M8 4.2a3.8 3.8 0 0 1 0 7.6z" fill="currentColor" />
        </>
      );
      break;
    case 'review':
      body = (
        <>
          {ring}
          <path d="M8 4.2a3.8 3.8 0 1 1 -3.8 3.8H8z" fill="currentColor" />
        </>
      );
      break;
    case 'blocked':
      body = (
        <>
          {ring}
          <path d="M5.2 10.8l5.6-5.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </>
      );
      break;
    case 'done':
    case 'closed':
      body = (
        <>
          <circle cx="8" cy="8" r="7" fill="currentColor" />
          <path d="M5 8.2l2 2 4-4.2" fill="none" style={{ stroke: 'var(--surface)' }} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </>
      );
      break;
    case 'cancelled':
      body = (
        <>
          <circle cx="8" cy="8" r="7" fill="currentColor" opacity="0.55" />
          <path d="M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8" style={{ stroke: 'var(--surface)' }} strokeWidth="1.6" strokeLinecap="round" />
        </>
      );
      break;
    default:
      body = <circle cx="8" cy="8" r="3" fill="currentColor" />;
  }
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      aria-hidden="true"
      focusable="false"
      className={`tk-st tk-st-${status} shrink-0`}
      data-status={status}
    >
      {body}
    </svg>
  );
}

const PRIORITY_BARS: Record<string, number> = { high: 3, medium: 2, low: 1 };

export function PriorityIcon({ priority }: { priority: string }) {
  if (priority === 'critical') {
    return (
      <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false" className="tk-prio-critical shrink-0">
        <rect x="1.5" y="1.5" width="13" height="13" rx="3" fill="currentColor" />
        <path d="M8 4.5v4.2M8 11.2v.3" style={{ stroke: 'var(--surface)' }} strokeWidth="2" strokeLinecap="round" />
      </svg>
    );
  }
  const n = PRIORITY_BARS[priority] ?? 0;
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" focusable="false" className="shrink-0">
      {[0, 1, 2].map((i) => (
        <rect
          key={i}
          x={2 + i * 4.5}
          y={10 - i * 3}
          width="3"
          height={4 + i * 3}
          rx="1"
          className={i < n ? 'tk-prio-on' : 'tk-prio-off'}
        />
      ))}
    </svg>
  );
}
