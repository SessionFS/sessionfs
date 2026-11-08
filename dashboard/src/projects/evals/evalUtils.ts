import type { Artifact, ArtifactLineage, ArtifactStage, EvalRun } from '../../api/client';

export interface LineageNode {
  id: string;
  depends_on: string[];
  artifact: Artifact;
}

const TEAM_TIERS = new Set(['team', 'enterprise', 'admin']);

/** Evaluation writes need Team or Enterprise on the project's own plan. Reads stay open. */
export function planAllowsEvals(tier: string | null | undefined): boolean {
  return !!tier && TEAM_TIERS.has(tier);
}

export const STAGES: ArtifactStage[] = ['candidate', 'staging', 'production', 'archived'];

export const KIND_LABEL: Record<string, string> = {
  dataset: 'Dataset',
  model: 'Model',
  feature_set: 'Feature set',
  prompt: 'Prompt',
  checkpoint: 'Checkpoint',
};

export const RELATION_LABEL: Record<string, string> = {
  derived_from: 'derived from',
  trained_on: 'trained on',
  fine_tuned_from: 'fine-tuned from',
  evaluated_against: 'evaluated against',
  uses: 'uses',
};

export const stageLabel = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

export function pct(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  return `${Math.round(v * 1000) / 10}%`;
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

/**
 * A link only when the server set `link_url` and it is https (hf:// arrives
 * already mapped to huggingface.co). Every other URI is copy-only: SessionFS
 * never fetches it and the dashboard never opens it.
 */
export function safeArtifactLink(a: Pick<Artifact, 'link_url'>): string | null {
  const url = a.link_url;
  if (!url) return null;
  try {
    return new URL(url).protocol === 'https:' ? url : null;
  } catch {
    return null;
  }
}

export const artifactLabel = (a: Pick<Artifact, 'name' | 'version'>) => `${a.name}@${a.version}`;

const GRADER_LABEL: Record<string, string> = {
  deterministic: 'Deterministic',
  llm_judge: 'LLM judge',
  human: 'Human',
  hybrid: 'Hybrid',
};

export const graderLabel = (k: string) => GRADER_LABEL[k] ?? k;

export const subjectOf = (r: Pick<EvalRun, 'artifact_id' | 'subject_label' | 'commit_sha'>) =>
  r.subject_label || r.artifact_id || (r.commit_sha ? r.commit_sha.slice(0, 12) : '—');

/** Parents of each node inside the returned subgraph (edges outside it are ignored). */
export function lineageNodes(lineage: ArtifactLineage): LineageNode[] {
  const ids = new Set(lineage.nodes.map((n) => n.id));
  return lineage.nodes.map((artifact) => ({
    id: artifact.id,
    artifact,
    depends_on: lineage.edges.filter((e) => e.child_id === artifact.id && ids.has(e.parent_id)).map((e) => e.parent_id),
  }));
}

/** Text form of the graph, for the accessible description and the list below it. */
export function describeLineage(lineage: ArtifactLineage): string {
  const byId = new Map(lineage.nodes.map((n) => [n.id, n]));
  const name = (id: string) => {
    const a = byId.get(id);
    return a ? artifactLabel(a) : id;
  };
  if (lineage.edges.length === 0) return `${name(lineage.root_id)} has no recorded parents or children.`;
  return lineage.edges.map((e) => `${name(e.child_id)} ${RELATION_LABEL[e.relation] ?? e.relation} ${name(e.parent_id)}`).join('; ');
}
