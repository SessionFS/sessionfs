import { useRulesVersion } from '../../hooks/useRules';
import { Button } from '../../components/ui';

/**
 * Loads a single version (cache-shared with the version viewer) and renders
 * one card per enabled tool with filename + token count + View.
 */
export function CompiledOutputsList({
  projectId,
  version,
  enabledTools,
  onView,
}: {
  projectId: string;
  version: number;
  enabledTools: string[];
  onView: (tool: string) => void;
}) {
  const { data: detail, isLoading } = useRulesVersion(projectId, version);

  if (isLoading) {
    return <p className="text-xs text-text-tertiary">Loading compiled outputs…</p>;
  }
  if (!detail) {
    return <p className="text-xs text-text-tertiary italic">No compiled outputs found.</p>;
  }

  const outputs = detail.compiled_outputs || {};
  const cards = enabledTools
    .map((tool) => ({ tool, output: outputs[tool] }))
    .filter((c) => c.output);

  if (cards.length === 0) {
    return (
      <p className="text-xs text-text-tertiary italic">
        No compiled outputs for the enabled tools in this version.
      </p>
    );
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
      {cards.map(({ tool, output }) => (
        <div
          key={tool}
          className="px-3 py-3 rounded-lg border border-border bg-bg-primary"
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="text-sm font-medium text-text-primary truncate">{tool}</p>
              <p className="text-xs font-mono text-text-tertiary truncate">
                {output!.filename}
              </p>
            </div>
            <span className="text-2xs text-text-tertiary shrink-0">
              {output!.token_count.toLocaleString()} tokens
            </span>
          </div>
          <div className="mt-2 flex justify-end">
            <Button variant="ghost" size="sm" onClick={() => onView(tool)}>
              View
            </Button>
          </div>
        </div>
      ))}
    </div>
  );
}
