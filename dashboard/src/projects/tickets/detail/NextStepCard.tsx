import type { ReactNode } from 'react';
import { Button } from '../../../components/ui';
import { Term } from '../../../help/Term';
import { getGlossaryEntry, type GlossaryId } from '../../../help/glossary';
import { PaneIcon } from './icons';
import type { NextStep, NextStepAction, NextStepTone } from './nextStep';

/* The one place in the pane that offers the next move. Tone is carried by
 * the left rule, the icon shape and the title together, never by colour
 * alone. Busy is the action currently being written. */

const TONE_VAR: Record<NextStepTone, string> = {
  warning: 'var(--warning)',
  success: 'var(--success)',
  brand: 'var(--brand)',
  danger: 'var(--danger)',
  info: 'var(--info)',
};

const TONE_TEXT: Record<NextStepTone, string> = {
  warning: 'text-warning',
  success: 'text-success',
  brand: 'text-brand',
  danger: 'text-danger',
  info: 'text-info',
};

function toneIcon(step: NextStep): ReactNode {
  switch (step.id) {
    case 'review_clean':
      return <PaneIcon name="shield" size={16} />;
    case 'children_finished':
      return <PaneIcon name="check" size={16} />;
    case 'issue_progress':
    case 'issue_empty':
      return <PaneIcon name="tree" size={16} />;
    case 'waiting_on_dependency':
    case 'blocked':
      return <PaneIcon name="lock" size={16} />;
    case 'overdue':
    case 'review_changes':
      return <PaneIcon name="warn" size={16} />;
    case 'in_progress':
    case 'ready':
      return <PaneIcon name="terminal" size={16} />;
    case 'unassigned':
      return <PaneIcon name="user" size={16} />;
    default:
      return <PaneIcon name="eye" size={16} />;
  }
}

/** The step text with its `{{id|words}}` markers as glossary terms. */
function withTerms(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(/\{\{([a-z0-9-]+)\|([^}]+)\}\}/g)) {
    out.push(text.slice(last, m.index));
    out.push(
      getGlossaryEntry(m[1]) ? (
        <Term key={m.index} id={m[1] as GlossaryId}>
          {m[2]}
        </Term>
      ) : (
        m[2]
      ),
    );
    last = (m.index ?? 0) + m[0].length;
  }
  out.push(text.slice(last));
  return out;
}

export function NextStepCard({
  step,
  onAction,
  busy,
}: {
  step: NextStep;
  onAction: (action: NextStepAction) => void;
  busy: NextStepAction['kind'] | null;
}) {
  const tone = TONE_VAR[step.tone];
  return (
    <section
      aria-label="Next step"
      data-next-step={step.id}
      className="flex gap-3 rounded-lg border border-border border-l-[3px] px-3 py-2.5"
      style={{
        borderLeftColor: tone,
        backgroundColor: `color-mix(in srgb, ${tone} 7%, var(--surface))`,
      }}
    >
      <span className={`mt-0.5 shrink-0 ${TONE_TEXT[step.tone]}`}>{toneIcon(step)}</span>
      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-sm font-semibold text-text-primary">{step.title}</p>
        <p className="text-xs leading-5 text-text-secondary">{withTerms(step.text)}</p>
        {step.cli && (
          <p className="text-xs text-text-tertiary">
            From the agent: <code className="text-mono-chip break-all">{step.cli}</code>
          </p>
        )}
        {step.actions.length > 0 && (
          <div className="flex flex-wrap gap-2 pt-1.5">
            {step.actions.map((a) => {
              const danger = a.variant === 'danger';
              return (
                <Button
                  key={`${a.kind}:${a.label}`}
                  variant={danger ? 'ghost' : a.variant}
                  size="sm"
                  className={danger ? '!text-danger' : undefined}
                  disabled={busy !== null}
                  loading={busy === a.kind && (a.kind === 'approve' || a.kind === 'dismiss' || a.kind === 'close')}
                  onClick={() => onAction(a)}
                >
                  {a.label}
                </Button>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
