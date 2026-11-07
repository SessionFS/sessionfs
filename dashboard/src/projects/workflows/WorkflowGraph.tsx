import { useId } from 'react';
import type { StepDefinition } from '../../api/workflows';
import { gateLabel, layoutSteps, NODE_H, NODE_W } from './graph';

const clip = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

/** The accessible description: the steps in order with what each depends on. */
export function describeSteps(steps: StepDefinition[]): string {
  return steps
    .map((s) => {
      const parts = [`${s.title} (${s.role}, ${s.agent}`];
      if (s.fan_out > 1) parts[0] += `, ${s.fan_out} in parallel`;
      const gate = gateLabel(s.gate);
      if (gate) parts[0] += `, ${gate.toLowerCase()}`;
      parts[0] += ')';
      if (s.depends_on.length) parts.push(`after ${s.depends_on.join(', ')}`);
      return parts.join(' ');
    })
    .join('; ');
}

/**
 * A read-only picture of a template: steps left to right by dependency,
 * each showing its title, role and agent, a ×N mark for fan-out and the gate.
 * Pure SVG with theme tokens. The graph keeps its natural width and scrolls
 * inside its own container, so it never widens the page at 400px.
 */
export function WorkflowGraph({ steps, label = 'Template graph' }: { steps: StepDefinition[]; label?: string }) {
  const id = useId();
  const layout = layoutSteps(steps);
  const byId = new Map(layout.nodes.map((n) => [n.step.id, n]));
  const description = describeSteps(steps);
  return (
    <div
      role="region"
      aria-label={label}
      tabIndex={0}
      data-testid="workflow-graph"
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
        {layout.edges.map((e) => {
          const a = byId.get(e.from)!;
          const b = byId.get(e.to)!;
          const x1 = a.x + NODE_W;
          const y1 = a.y + NODE_H / 2;
          const x2 = b.x;
          const y2 = b.y + NODE_H / 2;
          const mid = (x1 + x2) / 2;
          return (
            <path
              key={`${e.from}>${e.to}`}
              data-edge={`${e.from}>${e.to}`}
              d={`M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2 - 1} ${y2}`}
              fill="none"
              stroke="var(--border-strong)"
              strokeWidth="1.5"
              markerEnd={`url(#${id}-arrow)`}
            />
          );
        })}
        {layout.nodes.map(({ step, x, y }) => {
          const gate = gateLabel(step.gate);
          return (
            <g key={step.id} data-step={step.id} transform={`translate(${x} ${y})`}>
              <rect width={NODE_W} height={NODE_H} rx="8" fill="var(--surface)" stroke="var(--border-strong)" />
              <text x="10" y="21" fontSize="12" fontWeight="600" fill="var(--text-primary)">
                {clip(step.title, 24)}
              </text>
              <text x="10" y="39" fontSize="11" fill="var(--text-secondary)">
                {clip(`${step.role} · ${step.agent}`, 26)}
              </text>
              {step.fan_out > 1 && (
                <text x={NODE_W - 10} y="21" fontSize="11" fontWeight="600" textAnchor="end" fill="var(--brand)">
                  {`×${step.fan_out}`}
                </text>
              )}
              {gate && (
                <g>
                  <rect x="8" y={NODE_H - 26} width={NODE_W - 16} height="18" rx="9" fill="var(--bg-tertiary)" stroke="var(--border)" />
                  <text x={NODE_W / 2} y={NODE_H - 13} fontSize="10.5" textAnchor="middle" fill="var(--text-secondary)">
                    {clip(gate, 28)}
                  </text>
                </g>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
