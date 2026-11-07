import type { StepDefinition } from '../../api/workflows';

/** Layout constants shared by the SVG and its tests. */
export const NODE_W = 184;
export const NODE_H = 76;
export const GAP_X = 56;
export const GAP_Y = 18;
export const PAD = 12;

export interface PlacedStep {
  step: StepDefinition;
  column: number;
  row: number;
  x: number;
  y: number;
}
export interface Edge {
  from: string;
  to: string;
}
export interface GraphLayout {
  nodes: PlacedStep[];
  edges: Edge[];
  width: number;
  height: number;
}

/**
 * Steps left to right by dependency: a step sits one column right of its
 * deepest dependency. Within a column the template order is kept. Unknown
 * dependencies (a definition the server would refuse) are ignored, and a
 * cycle cannot loop forever because depth is memoised before recursing.
 */
export function layoutSteps(steps: StepDefinition[]): GraphLayout {
  const byId = new Map(steps.map((s) => [s.id, s]));
  const depth = new Map<string, number>();
  const visit = (id: string): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    depth.set(id, 0);
    const step = byId.get(id);
    let d = 0;
    for (const dep of step?.depends_on ?? []) {
      if (byId.has(dep)) d = Math.max(d, visit(dep) + 1);
    }
    depth.set(id, d);
    return d;
  };
  const rowsInColumn = new Map<number, number>();
  const nodes: PlacedStep[] = steps.map((step) => {
    const column = visit(step.id);
    const row = rowsInColumn.get(column) ?? 0;
    rowsInColumn.set(column, row + 1);
    return {
      step,
      column,
      row,
      x: PAD + column * (NODE_W + GAP_X),
      y: PAD + row * (NODE_H + GAP_Y),
    };
  });
  const edges: Edge[] = steps.flatMap((s) =>
    s.depends_on.filter((d) => byId.has(d)).map((d) => ({ from: d, to: s.id })),
  );
  const columns = Math.max(0, ...nodes.map((n) => n.column)) + 1;
  const rows = Math.max(1, ...Array.from(rowsInColumn.values()));
  return {
    nodes,
    edges,
    width: PAD * 2 + columns * NODE_W + (columns - 1) * GAP_X,
    height: PAD * 2 + rows * NODE_H + (rows - 1) * GAP_Y,
  };
}

export function gateLabel(gate: string): string | null {
  if (!gate || gate === 'none') return null;
  if (gate === 'trusted_clean') return 'Gate: trusted clean';
  if (gate.startsWith('approval_policy:')) return `Gate: approval ${gate.slice('approval_policy:'.length)}`;
  return `Gate: ${gate}`;
}
