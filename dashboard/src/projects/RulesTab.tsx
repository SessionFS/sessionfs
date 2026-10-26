import { useRef, useState } from 'react';
import {
  useProjectRules,
  useUpdateProjectRules,
  useCompileRules,
  useRulesVersions,
  useRulesVersion,
  isStaleEtagError,
} from '../hooks/useRules';
import { useToast } from '../hooks/useToast';
import RelativeDate from '../components/RelativeDate';
import type { ProjectRules } from '../api/client';
import { Button, Checkbox, Textarea } from '../components/ui';
import { CORE_TOOLS, CONTEXT_SECTIONS, KNOWLEDGE_TYPES } from './rules/constants';
import { VersionBadge } from './rules/VersionBadge';
import { DebouncedTokenInput, type DebouncedTokenInputHandle } from './rules/DebouncedTokenInput';
import { CompiledOutputsList } from './rules/CompiledOutputsList';
import { ToolOutputViewer, VersionViewerModal } from './rules/OutputViewers';
import { errorMessage } from '../utils/errorMessage';

export type { DebouncedTokenInputHandle };

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

/** `canEdit` is the project's `can_edit` flag: view-only readers see the
 *  rules and their compiled history but get no edit or compile controls. */
export default function RulesTab({
  projectId,
  canEdit = true,
}: {
  projectId: string;
  canEdit?: boolean;
}) {
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

  // Refs to the debounced Max tokens inputs so Compile can drain any
  // pending typed value before the compile mutation fires (otherwise
  // a fast typist → click can compile a stale server-side snapshot).
  const knowledgeTokensRef = useRef<DebouncedTokenInputHandle>(null);
  const contextTokensRef = useRef<DebouncedTokenInputHandle>(null);

  // Returns Promise<boolean>:  true = server committed, false = failed
  // (toast already surfaced). Gated follow-ups (Compile) check the
  // boolean and short-circuit on false instead of compiling against
  // a stale-server snapshot. Non-awaiting callers (checkbox toggles)
  // can ignore the return value — they still see the toast.
  //
  // The promise NEVER rejects: failure is signalled in-band via the
  // boolean so inline-arrow callers (`onChange={() => patchRules(…)}`)
  // don't generate unhandled-rejection warnings.
  function patchRules(partial: Partial<ProjectRules>): Promise<boolean> {
    if (!rules) return Promise.resolve(true);
    return updateRules.mutateAsync({ rules: partial, etag }).then(
      () => {
        addToast('success', 'Rules updated.');
        return true;
      },
      (err) => {
        if (isStaleEtagError(err)) {
          addToast(
            'error',
            'Rules have changed since you loaded them — refresh and try again.',
          );
        } else {
          addToast('error', `Update failed: ${errorMessage(err)}`);
        }
        return false;
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
            addToast('error', `Save failed: ${errorMessage(err)}`);
          }
        },
      },
    );
  }

  // Drain any pending debounced edits AND await their server commits
  // before firing the compile. Without the await, flush()'s mutate
  // and compile's mutate fly in parallel under HTTP/2 multiplexing,
  // so the backend can see compile-before-PUT and emit a stale build.
  //
  // If any flushed patch FAILED (409 stale-etag / network / server
  // error), short-circuit instead of compiling against the stale
  // server snapshot. The user already saw the patch-failure toast
  // from patchRules; we add a second toast explaining that compile
  // was skipped so the cause-effect chain is clear.
  async function handleCompile() {
    const results = await Promise.all([
      knowledgeTokensRef.current?.flush() ?? Promise.resolve(true),
      contextTokensRef.current?.flush() ?? Promise.resolve(true),
    ]);
    if (results.some((ok) => !ok)) {
      addToast(
        'error',
        'Compile skipped — pending rules update failed. Refresh and try again.',
      );
      return;
    }

    compileRules.mutate(undefined, {
      onSuccess: (result) => {
        if (result.created_new_version === false) {
          addToast('info', `No changes — still at v${result.version}.`);
        } else {
          addToast('success', `Compiled v${result.version}.`);
        }
      },
      onError: (err) => addToast('error', `Compile failed: ${errorMessage(err)}`),
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
    return <p className="p-5 text-text-tertiary text-sm">Loading rules…</p>;
  }

  if (error || !rules) {
    return (
      <div className="p-5">
        <p className="text-danger text-sm">
          Failed to load rules{error ? `: ${errorMessage(error)}` : ''}.
        </p>
      </div>
    );
  }

  const controlsLocked = updateRules.isPending || !canEdit;
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
            <h2 className="text-base font-semibold text-text-primary">
              Project Rules
            </h2>
            <VersionBadge version={rules.version} />
          </div>
          <p className="text-xs text-text-tertiary mt-0.5">
            Canonical rules for this project. Compile to generate tool-specific files. Updated{' '}
            <RelativeDate iso={rules.updated_at} />.
          </p>
        </div>
        {canEdit && (
          <Button
            onClick={handleCompile}
            disabled={compileRules.isPending || updateRules.isPending}
            loading={compileRules.isPending}
          >
            Compile
          </Button>
        )}
      </div>

      {/* Static preferences */}
      <section aria-labelledby="static-prefs-heading">
        <div className="flex items-center justify-between mb-2">
          <h3 id="static-prefs-heading" className="text-sm font-semibold text-text-primary">
            Static preferences
          </h3>
          {!editing && canEdit && (
            <Button variant="ghost" size="sm" onClick={() => setDraft(rules.static_rules ?? '')}>
              Edit
            </Button>
          )}
        </div>
        {editing && canEdit ? (
          <div>
            <Textarea
              value={draft ?? ''}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="Write canonical project preferences (markdown or plain text)…"
              className="min-h-[240px] font-mono text-base"
              autoFocus
            />
            <div className="flex justify-end gap-3 mt-3">
              <Button variant="ghost" onClick={() => setDraft(null)}>Cancel</Button>
              <Button onClick={handleSaveStatic} disabled={updateRules.isPending} loading={updateRules.isPending}>
                Save
              </Button>
            </div>
          </div>
        ) : (
          <pre className="px-3 py-3 text-sm font-mono bg-surface border border-border rounded-lg text-text-secondary whitespace-pre-wrap min-h-[80px]">
            {rules.static_rules || (
              <span className="text-text-tertiary italic">
                No static preferences set. Click Edit to add some.
              </span>
            )}
          </pre>
        )}
      </section>

      {/* Enabled tools */}
      <section aria-labelledby="tools-heading">
        <h3 id="tools-heading" className="text-sm font-semibold text-text-primary mb-2">
          Enabled tools
        </h3>
        <p className="text-xs text-text-tertiary mb-2">
          Compiled rule files will only be generated for tools you enable.
        </p>
        <div className="flex flex-wrap gap-3">
          {CORE_TOOLS.map((tool) => {
            const checked = rules.enabled_tools.includes(tool.value);
            return (
              <Checkbox
                key={tool.value}
                checked={checked}
                onChange={(e) => toggleTool(tool.value, e.target.checked)}
                disabled={controlsLocked}
                aria-label={`Enable ${tool.label}`}
                className="!items-center px-3 py-2 border border-border rounded-lg bg-bg-primary hover:bg-surface-hover transition-colors"
                label={
                  <>
                    {tool.label}
                    <span className="ml-2 text-2xs text-text-tertiary font-mono">
                      {tool.value}
                    </span>
                  </>
                }
              />
            );
          })}
        </div>
      </section>

      {/* Knowledge injection */}
      <section aria-labelledby="knowledge-heading">
        <h3 id="knowledge-heading" className="text-sm font-semibold text-text-primary mb-2">
          Knowledge injection
        </h3>
        <Checkbox
          checked={rules.include_knowledge}
          onChange={(e) => patchRules({ include_knowledge: e.target.checked })}
          disabled={controlsLocked}
          label="Include knowledge claims in compiled rules"
          className="!items-center mb-3"
        />
        {rules.include_knowledge && (
          <>
            <p className="text-xs text-text-tertiary mb-2">
              Only active, durable claims are injected. Select which entry types to include.
            </p>
            <div className="flex flex-wrap gap-2 mb-3">
              {KNOWLEDGE_TYPES.map((kt) => {
                const checked = rules.knowledge_types.includes(kt.value);
                return (
                  <Checkbox
                    key={kt.value}
                    checked={checked}
                    onChange={(e) => toggleKnowledgeType(kt.value, e.target.checked)}
                    disabled={controlsLocked}
                    aria-label={`Knowledge type ${kt.label}`}
                    className="!items-center px-2.5 py-1 border border-border rounded-full bg-bg-primary hover:bg-surface-hover transition-colors"
                    label={kt.label}
                  />
                );
              })}
            </div>
            <label className="flex items-center gap-2 text-xs text-text-secondary">
              Max tokens:
              <DebouncedTokenInput
                ref={knowledgeTokensRef}
                value={rules.knowledge_max_tokens}
                disabled={controlsLocked}
                ariaLabel="Knowledge injection max tokens"
                onCommit={(n) => patchRules({ knowledge_max_tokens: n })}
              />
            </label>
          </>
        )}
      </section>

      {/* Context injection */}
      <section aria-labelledby="context-heading">
        <h3 id="context-heading" className="text-sm font-semibold text-text-primary mb-2">
          Context injection
        </h3>
        <Checkbox
          checked={rules.include_context}
          onChange={(e) => patchRules({ include_context: e.target.checked })}
          disabled={controlsLocked}
          label="Include project context sections in compiled rules"
          className="!items-center mb-3"
        />
        {rules.include_context && (
          <>
            <p className="text-xs text-text-tertiary mb-2">
              Select which sections of the compiled context document to include.
            </p>
            <div className="flex flex-wrap gap-2 mb-3">
              {CONTEXT_SECTIONS.map((s) => {
                const checked = rules.context_sections.includes(s.value);
                return (
                  <Checkbox
                    key={s.value}
                    checked={checked}
                    onChange={(e) => toggleContextSection(s.value, e.target.checked)}
                    disabled={controlsLocked}
                    aria-label={`Context section ${s.label}`}
                    className="!items-center px-2.5 py-1 border border-border rounded-full bg-bg-primary hover:bg-surface-hover transition-colors"
                    label={s.label}
                  />
                );
              })}
            </div>
            <label className="flex items-center gap-2 text-xs text-text-secondary">
              Max tokens:
              <DebouncedTokenInput
                ref={contextTokensRef}
                value={rules.context_max_tokens}
                disabled={controlsLocked}
                ariaLabel="Context injection max tokens"
                onCommit={(n) => patchRules({ context_max_tokens: n })}
              />
            </label>
          </>
        )}
      </section>

      {/* Tool overrides (read-only) */}
      {rules.tool_overrides && Object.keys(rules.tool_overrides).length > 0 && (
        <section aria-labelledby="overrides-heading">
          <h3 id="overrides-heading" className="text-sm font-semibold text-text-primary mb-2">
            Tool overrides
            <span className="ml-2 text-xs font-normal text-text-tertiary">
              (read-only for v0.9.9)
            </span>
          </h3>
          <pre className="px-3 py-3 text-xs font-mono bg-bg-primary border border-border rounded-lg text-text-secondary whitespace-pre-wrap overflow-x-auto">
            {JSON.stringify(rules.tool_overrides, null, 2)}
          </pre>
        </section>
      )}

      {/* Compiled outputs (latest version) */}
      <section aria-labelledby="outputs-heading">
        <div className="flex items-center justify-between mb-2">
          <h3 id="outputs-heading" className="text-sm font-semibold text-text-primary">
            Compiled outputs
            {displayVersionNumber !== null && (
              <span className="ml-2 text-xs font-normal text-text-tertiary">
                {showingLatest ? '(latest — ' : '(viewing '}v{displayVersionNumber}
                {showingLatest ? ')' : ')'}
              </span>
            )}
          </h3>
          {!showingLatest && latestOutputsVersion !== null && (
            <Button variant="ghost" size="sm" onClick={() => setSelectedVersion(null)}>
              Show latest
            </Button>
          )}
        </div>
        {latestOutputsVersion === null ? (
          <p className="text-xs text-text-tertiary italic">
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
        <h3 id="versions-heading" className="text-sm font-semibold text-text-primary mb-2">
          Version history
        </h3>
        {versions.length === 0 ? (
          <p className="text-xs text-text-tertiary italic">No versions yet.</p>
        ) : (
          <div className="space-y-1.5">
            {versions.slice(0, 10).map((v) => (
              <button
                key={v.version}
                onClick={() => setSelectedVersion(v.version)}
                className="w-full flex items-center justify-between px-3 py-2 text-left rounded-lg border border-border bg-bg-primary hover:bg-surface-hover transition-colors"
              >
                <div className="flex items-center gap-3">
                  <VersionBadge version={v.version} />
                  <span className="text-xs font-mono text-text-tertiary">
                    {v.content_hash?.slice(0, 8)}
                  </span>
                </div>
                <span className="text-xs text-text-tertiary">
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
