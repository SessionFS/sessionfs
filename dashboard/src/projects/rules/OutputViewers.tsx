import { useState } from 'react';
import { useRulesVersion } from '../../hooks/useRules';
import RelativeDate from '../../components/RelativeDate';
import CopyButton from '../../components/CopyButton';
import type { RulesVersion } from '../../api/client';
import { Button, Dialog, DialogHeader } from '../../components/ui';
import { VersionBadge } from './VersionBadge';

interface CompiledOutputModalProps {
  tool: string;
  filename: string;
  content: string;
  tokenCount: number;
  onClose: () => void;
}

function CompiledOutputModal({ tool, filename, content, tokenCount, onClose }: CompiledOutputModalProps) {
  return (
    <Dialog open onClose={onClose} titleId="compiled-output-title" className="max-w-3xl max-h-[80vh] flex flex-col !p-0">
      <div className="flex items-center justify-between px-5 py-3 border-b border-border">
        <div className="min-w-0">
          <DialogHeader titleId="compiled-output-title">{filename}</DialogHeader>
          <p className="text-xs text-text-tertiary -mt-3">
            {tool} &middot; {tokenCount.toLocaleString()} tokens
          </p>
        </div>
        <div className="flex items-center gap-2">
          <CopyButton text={content} label="Copy" />
          <Button variant="ghost" onClick={onClose}>Close</Button>
        </div>
      </div>
      <pre className="flex-1 overflow-auto px-5 py-4 text-xs font-mono text-text-secondary whitespace-pre-wrap">
        {content}
      </pre>
    </Dialog>
  );
}

interface VersionViewerModalProps {
  version: RulesVersion;
  onClose: () => void;
}

export function VersionViewerModal({ version, onClose }: VersionViewerModalProps) {
  const entries = Object.entries(version.compiled_outputs || {});
  const [activeTool, setActiveTool] = useState<string | null>(entries[0]?.[0] ?? null);
  const active = activeTool ? version.compiled_outputs[activeTool] : null;
  return (
    <Dialog open onClose={onClose} titleId="version-viewer-title" className="max-w-4xl max-h-[85vh] flex flex-col !p-0">
      <div className="flex items-center justify-between px-5 py-3 border-b border-border">
        <div className="min-w-0">
          <DialogHeader titleId="version-viewer-title">
            <span className="inline-flex items-center gap-2">
              <VersionBadge version={version.version} />
              <span className="text-sm text-text-tertiary font-mono font-normal">
                {version.content_hash?.slice(0, 8)}
              </span>
            </span>
          </DialogHeader>
          <p className="text-xs text-text-tertiary -mt-3">
            Compiled <RelativeDate iso={version.compiled_at} />
          </p>
        </div>
        <Button variant="ghost" onClick={onClose}>Close</Button>
      </div>
      {entries.length === 0 ? (
        <p className="p-8 text-center text-sm text-text-tertiary">
          No compiled outputs in this version.
        </p>
      ) : (
        <>
          <div className="flex gap-1 px-5 pt-3 border-b border-border overflow-x-auto">
            {entries.map(([tool, out]) => (
              <button
                key={tool}
                onClick={() => setActiveTool(tool)}
                className={`px-3 py-2 text-sm font-medium whitespace-nowrap transition-colors ${
                  activeTool === tool
                    ? 'text-brand border-b-2 border-[var(--brand)]'
                    : 'text-text-tertiary hover:text-text-secondary'
                }`}
              >
                {tool}
                <span className="ml-1.5 text-2xs text-text-tertiary">
                  ({out.token_count.toLocaleString()}t)
                </span>
              </button>
            ))}
          </div>
          {active && (
            <>
              <div className="flex items-center justify-between px-5 py-2 text-xs text-text-tertiary border-b border-border">
                <span className="font-mono">{active.filename}</span>
                <CopyButton text={active.content} label="Copy" />
              </div>
              <pre className="flex-1 overflow-auto px-5 py-4 text-xs font-mono text-text-secondary whitespace-pre-wrap">
                {active.content}
              </pre>
            </>
          )}
        </>
      )}
    </Dialog>
  );
}

/**
 * Fetches the version once more (query cache will hit) and renders the
 * CompiledOutputModal for the chosen tool.
 */
export function ToolOutputViewer({
  projectId,
  version,
  tool,
  onClose,
}: {
  projectId: string;
  version: number;
  tool: string;
  onClose: () => void;
}) {
  const { data: detail } = useRulesVersion(projectId, version);
  const output = detail?.compiled_outputs?.[tool];
  if (!output) return null;
  return (
    <CompiledOutputModal
      tool={tool}
      filename={output.filename}
      content={output.content}
      tokenCount={output.token_count}
      onClose={onClose}
    />
  );
}

