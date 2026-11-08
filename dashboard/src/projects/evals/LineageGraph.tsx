import { useId } from 'react';
import type { ArtifactLineage, EvalRunVerdict } from '../../api/client';
import { layoutSteps, NODE_H, NODE_W } from '../workflows/graph';
import { artifactLabel, describeLineage, KIND_LABEL, lineageNodes, RELATION_LABEL, stageLabel } from './evalUtils';

const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

function evalLine(verdicts: (EvalRunVerdict | null)[]): string {
  if (verdicts.length === 0) return 'No trusted runs';
  const passed = verdicts.filter((v) => v === 'passed').length;
  const other = verdicts.length - passed;
  return [passed ? `${passed} passed` : '', other ? `${other} not passed` : ''].filter(Boolean).join(' · ');
}

/**
 * Parents on the left, children on the right, laid out with the workflow
 * graph's DAG layout. Pure SVG with theme tokens; it keeps its natural width
 * and scrolls inside its own container so it never widens the page.
 * The node list under it is the keyboard way in: each item opens that artifact.
 */
export function LineageGraph({ lineage, onOpen }: { lineage: ArtifactLineage; onOpen: (id: string) => void }) {
  const id = useId();
  const nodes = lineageNodes(lineage);
  const layout = layoutSteps(nodes);
  const byId = new Map(layout.nodes.map((n) => [n.step.id, n]));
  const verdicts = new Map<string, (EvalRunVerdict | null)[]>();
  for (const e of lineage.evals) verdicts.set(e.artifact_id, [...(verdicts.get(e.artifact_id) ?? []), e.verdict]);
  const description = describeLineage(lineage);
  const name = (nid: string) => {
    const a = byId.get(nid)?.step.artifact;
    return a ? artifactLabel(a) : nid;
  };
  return (
    <div className="space-y-3">
      <div
        role="region"
        aria-label="Lineage graph"
        tabIndex={0}
        data-testid="lineage-graph"
        className="max-w-full overflow-x-auto rounded-md border border-border bg-bg-sunken p-1 focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
      >
        <svg
          role="img"
          aria-labelledby={`${id}-desc`}
          width={layout.width}
          height={layout.height}
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          className="block max-w-none"
        >
          <title id={`${id}-desc`}>{description}</title>
          <defs>
            <marker id={`${id}-arrow`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
              <path d="M0 0 L8 4 L0 8 z" fill="var(--border-strong)" />
            </marker>
          </defs>
          {lineage.edges.map((e) => {
            const a = byId.get(e.parent_id);
            const b = byId.get(e.child_id);
            if (!a || !b) return null;
            const x1 = a.x + NODE_W;
            const y1 = a.y + NODE_H / 2;
            const x2 = b.x;
            const y2 = b.y + NODE_H / 2;
            const mid = (x1 + x2) / 2;
            return (
              <g key={e.id} data-edge={`${e.parent_id}>${e.child_id}`}>
                <path
                  d={`M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2 - 1} ${y2}`}
                  fill="none"
                  stroke="var(--border-strong)"
                  strokeWidth="1.5"
                  markerEnd={`url(#${id}-arrow)`}
                />
              </g>
            );
          })}
          {layout.nodes.map(({ step, x, y }) => {
            const a = step.artifact;
            const root = a.id === lineage.root_id;
            return (
              <g key={a.id} data-node={a.id} transform={`translate(${x} ${y})`}>
                <rect
                  width={NODE_W}
                  height={NODE_H}
                  rx="8"
                  fill="var(--surface)"
                  stroke={root ? 'var(--brand)' : 'var(--border-strong)'}
                  strokeWidth={root ? 2 : 1}
                />
                <text x="10" y="21" fontSize="12" fontWeight="600" fill="var(--text-primary)">
                  {clip(artifactLabel(a), 26)}
                </text>
                <text x="10" y="40" fontSize="11" fill="var(--text-secondary)">
                  {clip(`${KIND_LABEL[a.kind] ?? a.kind} · ${stageLabel(a.stage)}${a.deprecated_at ? ' · deprecated' : ''}`, 30)}
                </text>
                <text x="10" y="58" fontSize="11" fill="var(--text-tertiary)">
                  {clip(evalLine(verdicts.get(a.id) ?? []), 30)}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      {lineage.truncated && (
        <p className="text-xs text-warning">The lineage is larger than one view; only the nearest 500 artifacts are shown.</p>
      )}
      <ul className="space-y-1 text-sm" aria-label="Lineage links">
        {lineage.edges.map((e) => (
          <li key={e.id} className="flex min-w-0 flex-wrap items-center gap-x-1.5">
            <NodeButton label={name(e.child_id)} onClick={() => onOpen(e.child_id)} current={e.child_id === lineage.root_id} />
            <span className="text-text-tertiary">{RELATION_LABEL[e.relation] ?? e.relation}</span>
            <NodeButton label={name(e.parent_id)} onClick={() => onOpen(e.parent_id)} current={e.parent_id === lineage.root_id} />
            {e.context_ref && <span className="break-all font-mono text-2xs text-text-muted">({e.context_ref})</span>}
          </li>
        ))}
        {lineage.edges.length === 0 && <li className="text-text-tertiary">No parents or children recorded yet.</li>}
      </ul>
    </div>
  );
}

function NodeButton({ label, onClick, current }: { label: string; onClick: () => void; current: boolean }) {
  if (current) return <span className="break-all font-mono text-xs font-semibold text-text-primary">{label}</span>;
  return (
    <button
      type="button"
      onClick={onClick}
      className="break-all text-left font-mono text-xs text-brand underline-offset-2 hover:underline focus-visible:outline-none focus-visible:shadow-[0_0_0_3px_var(--brand-glow)]"
    >
      {label}
    </button>
  );
}
