import { useRef, useState } from 'react';
import {
  useProjectRules,
  useUpdateProjectRules,
  useCompileRules,
  useRulesVersions,
  useRulesVersion,
  isStaleEtagError,
} from '../hooks/useRules';
import { useFocusTrap } from '../hooks/useFocusTrap';
import { useToast } from '../hooks/useToast';
import RelativeDate from '../components/RelativeDate';
import CopyButton from '../components/CopyButton';
import type { ProjectRules, RulesVersion } from '../api/client';

/**
 * Rules tab — v0.9.9.
 *
 * Canonical rules live on the project. The tab surfaces:
 *   - current version badge + updated_at
 *   - static preferences (read/edit)
 *   - enabled tools (checkboxes)
 *   - knowledge injection settings
 *   - context injection settings
 *   - compiled outputs per tool (view modal, with copy)
 *   - last N versions (click to view)
 *   - compile action
 *
 * Deferred (per brief): rich diff viewer, suggestion moderation, tool-override editor.
 */

const CORE_TOOLS: { value: string; label: string }[] = [
  { value: 'claude-code', label: 'Claude Code' },
  { value: 'codex', label: 'Codex' },
  { value: 'cursor', label: 'Cursor' },
  { value: 'copilot', label: 'Copilot' },
  { value: 'gemini', label: 'Gemini' },
];

const KNOWLEDGE_TYPES: { value: string; label: string }[] = [
  { value: 'decision', label: 'Decision' },
  { value: 'pattern', label: 'Pattern' },
  { value: 'convention', label: 'Convention' },
  { value: 'discovery', label: 'Discovery' },
  { value: 'bug', label: 'Bug' },
  { value: 'dependency', label: 'Dependency' },
];

const CONTEXT_SECTIONS: { value: string; label: string }[] = [
  { value: 'overview', label: 'Overview' },
  { value: 'architecture', label: 'Architecture' },
  { value: 'conventions', label: 'Conventions' },
  { value: 'patterns', label: 'Patterns' },
  { value: 'dependencies', label: 'Dependencies' },
];

function VersionBadge({ version }: { version: number }) {
  return (
    <span
      className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold"
      style={{
        backgroundColor: 'var(--brand)',
        color: '#fff',
      }}
    >
      v{version}
    </span>
  );
}

interface CompiledOutputModalProps {
  tool: string;
  filename: string;
  content: string;
  tokenCount: number;
  onClose: () => void;
}

function CompiledOutputModal({ tool, filename, content, tokenCount, onClose }: CompiledOutputModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(dialogRef);
  return (
    <>
      <div
        className="fixed inset-0 z-50 bg-black/50"
        onClick={onClose}
        role="presentation"
      />
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4 pointer-events-none">
        <div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="compiled-output-title"
          onKeyDown={(e) => {
            if (e.key === 'Escape') onClose();
          }}
          className="pointer-events-auto w-full max-w-3xl max-h-[80vh] flex flex-col bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-lg)]"
        >
          <div className="flex items-center justify-between px-5 py-3 border-b border-[var(--border)]">
            <div className="min-w-0">
              <h3 id="compiled-output-title" className="text-base font-semibold text-[var(--text-primary)]">
                {filename}
              </h3>
              <p className="text-xs text-[var(--text-tertiary)]">
                {tool} &middot; {tokenCount.toLocaleString()} tokens
              </p>
            </div>
            <div className="flex items-center gap-2">
              <CopyButton text={content} label="Copy" />
              <button
                onClick={onClose}
                className="px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
              >
                Close
              </button>
            </div>
          </div>
          <pre className="flex-1 overflow-auto px-5 py-4 text-xs font-mono text-[var(--text-secondary)] whitespace-pre-wrap">
            {content}
          </pre>
        </div>
      </div>
    </>
  );
}

interface VersionViewerModalProps {
  version: RulesVersion;
  onClose: () => void;
}

function VersionViewerModal({ version, onClose }: VersionViewerModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useFocusTrap(dialogRef);
  const entries = Object.entries(version.compiled_outputs || {});
  const [activeTool, setActiveTool] = useState<string | null>(entries[0]?.[0] ?? null);
  const active = activeTool ? version.compiled_outputs[activeTool] : null;
  return (
    <>
      <div className="fixed inset-0 z-50 bg-black/50" onClick={onClose} role="presentation" />
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4 pointer-events-none">
        <div
          ref={dialogRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="version-viewer-title"
          onKeyDown={(e) => {
            if (e.key === 'Escape') onClose();
          }}
          className="pointer-events-auto w-full max-w-4xl max-h-[85vh] flex flex-col bg-[var(--bg-elevated)] border border-[var(--border)] rounded-xl shadow-[var(--shadow-lg)]"
        >
          <div className="flex items-center justify-between px-5 py-3 border-b border-[var(--border)]">
            <div className="min-w-0">
              <h3 id="version-viewer-title" className="text-base font-semibold text-[var(--text-primary)] flex items-center gap-2">
                <VersionBadge version={version.version} />
                <span className="text-sm text-[var(--text-tertiary)] font-mono">
                  {version.content_hash?.slice(0, 8)}
                </span>
              </h3>
              <p className="text-xs text-[var(--text-tertiary)] mt-0.5">
                Compiled <RelativeDate iso={version.compiled_at} />
              </p>
            </div>
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
            >
              Close
            </button>
          </div>
          {entries.length === 0 ? (
            <p className="p-8 text-center text-sm text-[var(--text-tertiary)]">
              No compiled outputs in this version.
            </p>
          ) : (
            <>
              <div className="flex gap-1 px-5 pt-3 border-b border-[var(--border)] overflow-x-auto">
                {entries.map(([tool, out]) => (
                  <button
                    key={tool}
                    onClick={() => setActiveTool(tool)}
                    className={`px-3 py-2 text-sm font-medium whitespace-nowrap transition-colors ${
                      activeTool === tool
                        ? 'text-[var(--brand)] border-b-2 border-[var(--brand)]'
                        : 'text-[var(--text-tertiary)] hover:text-[var(--text-secondary)]'
                    }`}
                  >
                    {tool}
                    <span className="ml-1.5 text-[10px] text-[var(--text-tertiary)]">
                      ({out.token_count.toLocaleString()}t)
                    </span>
                  </button>
                ))}
              </div>
              {active && (
                <>
                  <div className="flex items-center justify-between px-5 py-2 text-xs text-[var(--text-tertiary)] border-b border-[var(--border)]">
                    <span className="font-mono">{active.filename}</span>
                    <CopyButton text={active.content} label="Copy" />
                  </div>
                  <pre className="flex-1 overflow-auto px-5 py-4 text-xs font-mono text-[var(--text-secondary)] whitespace-pre-wrap">
                    {active.content}
                  </pre>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}

export default function RulesTab({ projectId }: { projectId: string }) {
  const { data: rulesResp, isLoading, error } = useProjectRules(projectId);
  const { data: versionsResp } = useRulesVersions(projectId);
  const updateRules = useUpdateProjectRules(projectId);
  const compileRules = useCompileRules(projectId);
  const { addToast } = useToast();

  const rules: ProjectRules | undefined = rulesResp?.data;
  const etag = rulesResp?.etag ?? '';

  // Local draft for static_rules editor. `null` means "not editing";
  // otherwise holds the in-progress draft string.
  const [draft, setDraft] = useState<string | null>(null);
  const editing = draft !== null;

  // Viewer state.
  const [viewToolOutput, setViewToolOutput] = useState<string | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const { data: versionDetail } = useRulesVersion(projectId, selectedVersion);

  function patchRules(partial: Partial<ProjectRules>) {
    if (!rules) return;
    updateRules.mutate(
      { rules: partial, etag },
      {
        onSuccess: () => addToast('success', 'Rules updated.'),
        onError: (err) => {
          if (isStaleEtagError(err)) {
            addToast(
              'error',
              'Rules have changed since you loaded them — refresh and try again.',
            );
          } else {
            addToast('error', `Update failed: ${String(err)}`);
          }
        },
      },
    );
  }

  function handleSaveStatic() {
    if (!rules || draft === null) return;
    updateRules.mutate(
      { rules: { static_rules: draft }, etag },
      {
        onSuccess: () => {
          addToast('success', 'Static preferences saved.');
          setDraft(null);
        },
        onError: (err) => {
          if (isStaleEtagError(err)) {
            addToast(
              'error',
              'Rules have changed since you loaded them — refresh and try again.',
            );
          } else {
            addToast('error', `Save failed: ${String(err)}`);
          }
        },
      },
    );
  }

  function handleCompile() {
    compileRules.mutate(undefined, {
      onSuccess: (result) => {
        if (result.created_new_version === false) {
          addToast('info', `No changes — still at v${result.version}.`);
        } else {
          addToast('success', `Compiled v${result.version}.`);
        }
      },
      onError: (err) => addToast('error', `Compile failed: ${String(err)}`),
    });
  }

  function toggleTool(tool: string, checked: boolean) {
    if (!rules) return;
    const next = checked
      ? Array.from(new Set([...rules.enabled_tools, tool]))
      : rules.enabled_tools.filter((t) => t !== tool);
    patchRules({ enabled_tools: next });
  }

  function toggleKnowledgeType(type: string, checked: boolean) {
    if (!rules) return;
    const next = checked
      ? Array.from(new Set([...rules.knowledge_types, type]))
      : rules.knowledge_types.filter((t) => t !== type);
    patchRules({ knowledge_types: next });
  }

  function toggleContextSection(section: string, checked: boolean) {
    if (!rules) return;
    const next = checked
      ? Array.from(new Set([...rules.context_sections, section]))
      : rules.context_sections.filter((s) => s !== section);
    patchRules({ context_sections: next });
  }

  if (isLoading) {
    return <p className="p-5 text-[var(--text-tertiary)] text-sm">Loading rules...</p>;
  }

  if (error || !rules) {
    return (
      <div className="p-5">
        <p className="text-red-400 text-sm">
          Failed to load rules{error ? `: ${String(error)}` : ''}.
        </p>
      </div>
    );
  }

  const versions = versionsResp?.versions ?? [];
  const latestVersion = versions[0];
  // Latest compiled outputs come from fetching the latest version detail.
  // We piggy-back on the selectedVersion query to show outputs for the
  // latest version by default when the user hasn't picked another one.
  const latestOutputsVersion = latestVersion?.version ?? null;
  const showingLatest = selectedVersion === null && latestOutputsVersion !== null;
  const displayVersionNumber = selectedVersion ?? latestOutputsVersion;

  return (
    <div className="p-5 space-y-6">
      {/* Header: current version + compile */}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-base font-semibold text-[var(--text-primary)]">
              Project Rules
            </h2>
            <VersionBadge version={rules.version} />
          </div>
          <p className="text-xs text-[var(--text-tertiary)] mt-0.5">
            Canonical rules for this project. Compile to generate tool-specific files. Updated{' '}
            <RelativeDate iso={rules.updated_at} />.
          </p>
        </div>
        <button
          onClick={handleCompile}
          disabled={compileRules.isPending}
          className="px-4 py-2 text-sm font-semibold bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {compileRules.isPending ? 'Compiling...' : 'Compile'}
        </button>
      </div>

      {/* Static preferences */}
      <section aria-labelledby="static-prefs-heading">
        <div className="flex items-center justify-between mb-2">
          <h3 id="static-prefs-heading" className="text-sm font-semibold text-[var(--text-primary)]">
            Static preferences
          </h3>
          {!editing && (
            <button
              onClick={() => setDraft(rules.static_rules ?? '')}
              className="text-xs text-[var(--brand)] hover:underline"
            >
              Edit
            </button>
          )}
        </div>
        {editing ? (
          <div>
            <textarea
              value={draft ?? ''}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="Write canonical project preferences (markdown or plain text)..."
              className="w-full min-h-[240px] px-3 py-3 text-[14px] font-mono bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)] resize-y placeholder:text-[var(--text-tertiary)]"
              autoFocus
            />
            <div className="flex justify-end gap-3 mt-3">
              <button
                onClick={() => setDraft(null)}
                className="px-3 py-1.5 text-sm text-[var(--text-secondary)] hover:text-[var(--text-primary)] transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleSaveStatic}
                disabled={updateRules.isPending}
                className="px-5 py-2 text-sm font-semibold bg-[var(--brand)] text-white rounded-lg hover:bg-[var(--brand-hover)] transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {updateRules.isPending ? 'Saving...' : 'Save'}
              </button>
            </div>
          </div>
        ) : (
          <pre className="px-3 py-3 text-[13px] font-mono bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-secondary)] whitespace-pre-wrap min-h-[80px]">
            {rules.static_rules || (
              <span className="text-[var(--text-tertiary)] italic">
                No static preferences set. Click Edit to add some.
              </span>
            )}
          </pre>
        )}
      </section>

      {/* Enabled tools */}
      <section aria-labelledby="tools-heading">
        <h3 id="tools-heading" className="text-sm font-semibold text-[var(--text-primary)] mb-2">
          Enabled tools
        </h3>
        <p className="text-xs text-[var(--text-tertiary)] mb-2">
          Compiled rule files will only be generated for tools you enable.
        </p>
        <div className="flex flex-wrap gap-3">
          {CORE_TOOLS.map((tool) => {
            const checked = rules.enabled_tools.includes(tool.value);
            return (
              <label
                key={tool.value}
                className="flex items-center gap-2 px-3 py-2 border border-[var(--border)] rounded-lg bg-[var(--bg-primary)] cursor-pointer hover:bg-[var(--surface-hover)] transition-colors"
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={(e) => toggleTool(tool.value, e.target.checked)}
                  disabled={updateRules.isPending}
                  className="accent-[var(--brand)]"
                  aria-label={`Enable ${tool.label}`}
                />
                <span className="text-sm text-[var(--text-primary)]">{tool.label}</span>
                <span className="text-[10px] text-[var(--text-tertiary)] font-mono">
                  {tool.value}
                </span>
              </label>
            );
          })}
        </div>
      </section>

      {/* Knowledge injection */}
      <section aria-labelledby="knowledge-heading">
        <h3 id="knowledge-heading" className="text-sm font-semibold text-[var(--text-primary)] mb-2">
          Knowledge injection
        </h3>
        <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)] mb-3">
          <input
            type="checkbox"
            checked={rules.include_knowledge}
            onChange={(e) => patchRules({ include_knowledge: e.target.checked })}
            disabled={updateRules.isPending}
            className="accent-[var(--brand)]"
          />
          Include knowledge claims in compiled rules
        </label>
        {rules.include_knowledge && (
          <>
            <p className="text-xs text-[var(--text-tertiary)] mb-2">
              Only active, durable claims are injected. Select which entry types to include.
            </p>
            <div className="flex flex-wrap gap-2 mb-3">
              {KNOWLEDGE_TYPES.map((kt) => {
                const checked = rules.knowledge_types.includes(kt.value);
                return (
                  <label
                    key={kt.value}
                    className="flex items-center gap-1.5 px-2.5 py-1 text-xs border border-[var(--border)] rounded-full bg-[var(--bg-primary)] cursor-pointer hover:bg-[var(--surface-hover)] transition-colors"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(e) => toggleKnowledgeType(kt.value, e.target.checked)}
                      disabled={updateRules.isPending}
                      className="accent-[var(--brand)]"
                      aria-label={`Knowledge type ${kt.label}`}
                    />
                    <span className="text-[var(--text-primary)]">{kt.label}</span>
                  </label>
                );
              })}
            </div>
            <label className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
              Max tokens:
              <input
                type="number"
                min={0}
                step={500}
                value={rules.knowledge_max_tokens}
                onChange={(e) => {
                  const n = parseInt(e.target.value, 10);
                  if (!Number.isNaN(n) && n >= 0) {
                    patchRules({ knowledge_max_tokens: n });
                  }
                }}
                disabled={updateRules.isPending}
                className="w-24 px-2 py-1 text-xs bg-[var(--bg-primary)] border border-[var(--border)] rounded text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)]"
              />
            </label>
          </>
        )}
      </section>

      {/* Context injection */}
      <section aria-labelledby="context-heading">
        <h3 id="context-heading" className="text-sm font-semibold text-[var(--text-primary)] mb-2">
          Context injection
        </h3>
        <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)] mb-3">
          <input
            type="checkbox"
            checked={rules.include_context}
            onChange={(e) => patchRules({ include_context: e.target.checked })}
            disabled={updateRules.isPending}
            className="accent-[var(--brand)]"
          />
          Include project context sections in compiled rules
        </label>
        {rules.include_context && (
          <>
            <p className="text-xs text-[var(--text-tertiary)] mb-2">
              Select which sections of the compiled context document to include.
            </p>
            <div className="flex flex-wrap gap-2 mb-3">
              {CONTEXT_SECTIONS.map((s) => {
                const checked = rules.context_sections.includes(s.value);
                return (
                  <label
                    key={s.value}
                    className="flex items-center gap-1.5 px-2.5 py-1 text-xs border border-[var(--border)] rounded-full bg-[var(--bg-primary)] cursor-pointer hover:bg-[var(--surface-hover)] transition-colors"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={(e) => toggleContextSection(s.value, e.target.checked)}
                      disabled={updateRules.isPending}
                      className="accent-[var(--brand)]"
                      aria-label={`Context section ${s.label}`}
                    />
                    <span className="text-[var(--text-primary)]">{s.label}</span>
                  </label>
                );
              })}
            </div>
            <label className="flex items-center gap-2 text-xs text-[var(--text-secondary)]">
              Max tokens:
              <input
                type="number"
                min={0}
                step={500}
                value={rules.context_max_tokens}
                onChange={(e) => {
                  const n = parseInt(e.target.value, 10);
                  if (!Number.isNaN(n) && n >= 0) {
                    patchRules({ context_max_tokens: n });
                  }
                }}
                disabled={updateRules.isPending}
                className="w-24 px-2 py-1 text-xs bg-[var(--bg-primary)] border border-[var(--border)] rounded text-[var(--text-primary)] focus:outline-none focus:border-[var(--brand)]"
              />
            </label>
          </>
        )}
      </section>

      {/* Tool overrides (read-only) */}
      {rules.tool_overrides && Object.keys(rules.tool_overrides).length > 0 && (
        <section aria-labelledby="overrides-heading">
          <h3 id="overrides-heading" className="text-sm font-semibold text-[var(--text-primary)] mb-2">
            Tool overrides
            <span className="ml-2 text-xs font-normal text-[var(--text-tertiary)]">
              (read-only for v0.9.9)
            </span>
          </h3>
          <pre className="px-3 py-3 text-[12px] font-mono bg-[var(--bg-primary)] border border-[var(--border)] rounded-lg text-[var(--text-secondary)] whitespace-pre-wrap overflow-x-auto">
            {JSON.stringify(rules.tool_overrides, null, 2)}
          </pre>
        </section>
      )}

      {/* Compiled outputs (latest version) */}
      <section aria-labelledby="outputs-heading">
        <div className="flex items-center justify-between mb-2">
          <h3 id="outputs-heading" className="text-sm font-semibold text-[var(--text-primary)]">
            Compiled outputs
            {displayVersionNumber !== null && (
              <span className="ml-2 text-xs font-normal text-[var(--text-tertiary)]">
                {showingLatest ? '(latest — ' : '(viewing '}v{displayVersionNumber}
                {showingLatest ? ')' : ')'}
              </span>
            )}
          </h3>
          {!showingLatest && latestOutputsVersion !== null && (
            <button
              onClick={() => setSelectedVersion(null)}
              className="text-xs text-[var(--brand)] hover:underline"
            >
              Show latest
            </button>
          )}
        </div>
        {latestOutputsVersion === null ? (
          <p className="text-xs text-[var(--text-tertiary)] italic">
            No versions compiled yet. Click Compile to generate the first version.
          </p>
        ) : (
          <CompiledOutputsList
            projectId={projectId}
            version={displayVersionNumber!}
            enabledTools={rules.enabled_tools}
            onView={(tool) => setViewToolOutput(tool)}
          />
        )}
      </section>

      {/* Version list */}
      <section aria-labelledby="versions-heading">
        <h3 id="versions-heading" className="text-sm font-semibold text-[var(--text-primary)] mb-2">
          Version history
        </h3>
        {versions.length === 0 ? (
          <p className="text-xs text-[var(--text-tertiary)] italic">No versions yet.</p>
        ) : (
          <div className="space-y-1.5">
            {versions.slice(0, 10).map((v) => (
              <button
                key={v.version}
                onClick={() => setSelectedVersion(v.version)}
                className="w-full flex items-center justify-between px-3 py-2 text-left rounded-lg border border-[var(--border)] bg-[var(--bg-primary)] hover:bg-[var(--surface-hover)] transition-colors"
              >
                <div className="flex items-center gap-3">
                  <VersionBadge version={v.version} />
                  <span className="text-xs font-mono text-[var(--text-tertiary)]">
                    {v.content_hash?.slice(0, 8)}
                  </span>
                </div>
                <span className="text-xs text-[var(--text-tertiary)]">
                  <RelativeDate iso={v.compiled_at} />
                </span>
              </button>
            ))}
          </div>
        )}
      </section>

      {/* Compiled-output viewer (from enabled-tool card) */}
      {viewToolOutput && (
        <ToolOutputViewer
          projectId={projectId}
          version={displayVersionNumber!}
          tool={viewToolOutput}
          onClose={() => setViewToolOutput(null)}
        />
      )}

      {/* Version viewer modal (from version list) */}
      {selectedVersion !== null && versionDetail && (
        <VersionViewerModal version={versionDetail} onClose={() => setSelectedVersion(null)} />
      )}
    </div>
  );
}

/**
 * Loads a single version (cache-shared with the version viewer) and renders
 * one card per enabled tool with filename + token count + View.
 */
function CompiledOutputsList({
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
    return <p className="text-xs text-[var(--text-tertiary)]">Loading compiled outputs...</p>;
  }
  if (!detail) {
    return <p className="text-xs text-[var(--text-tertiary)] italic">No compiled outputs found.</p>;
  }

  const outputs = detail.compiled_outputs || {};
  const cards = enabledTools
    .map((tool) => ({ tool, output: outputs[tool] }))
    .filter((c) => c.output);

  if (cards.length === 0) {
    return (
      <p className="text-xs text-[var(--text-tertiary)] italic">
        No compiled outputs for the enabled tools in this version.
      </p>
    );
  }

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
      {cards.map(({ tool, output }) => (
        <div
          key={tool}
          className="px-3 py-3 rounded-lg border border-[var(--border)] bg-[var(--bg-primary)]"
        >
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="text-sm font-medium text-[var(--text-primary)] truncate">{tool}</p>
              <p className="text-xs font-mono text-[var(--text-tertiary)] truncate">
                {output!.filename}
              </p>
            </div>
            <span className="text-[10px] text-[var(--text-tertiary)] shrink-0">
              {output!.token_count.toLocaleString()} tokens
            </span>
          </div>
          <div className="mt-2 flex justify-end">
            <button
              onClick={() => onView(tool)}
              className="text-xs text-[var(--brand)] hover:underline"
            >
              View
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * Fetches the version once more (query cache will hit) and renders the
 * CompiledOutputModal for the chosen tool.
 */
function ToolOutputViewer({
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
