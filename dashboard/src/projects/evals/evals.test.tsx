import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../../api/client';
import { axe } from '../../test/axe';
import EvalsTab from './EvalsTab';
import ArtifactsTab, { PromotionPanel } from './ArtifactsTab';
import { LineageGraph } from './LineageGraph';
import { ArtifactUri } from './format';
import { describeLineage, lineageNodes, planAllowsEvals, safeArtifactLink } from './evalUtils';
import { layoutSteps } from '../workflows/graph';
import * as fx from './fixtures';

const { hooks } = vi.hoisted(() => ({
  hooks: {
    useEvalSuites: vi.fn(),
    useEvalSuite: vi.fn(),
    useEvalSuiteCases: vi.fn(),
    useEvalRuns: vi.fn(),
    useEvalRun: vi.fn(),
    useEvalResults: vi.fn(),
    useEvalCompare: vi.fn(),
    useVoidEvalRun: vi.fn(),
    useArtifactFamilies: vi.fn(),
    useArtifacts: vi.fn(),
    useArtifact: vi.fn(),
    useArtifactLineage: vi.fn(),
    usePromotionCheck: vi.fn(),
    usePromoteArtifact: vi.fn(),
    useDeprecateArtifact: vi.fn(),
    useFamilyRequirements: vi.fn(),
    useSaveFamilyRequirements: vi.fn(),
    useFamilyEvals: vi.fn(),
    useSuiteNames: vi.fn(),
  },
}));
vi.mock('../../hooks/useEvals', () => hooks);

const ok = (data: unknown) => ({ data, isLoading: false, error: null });
const pages = (page: unknown) => ({ data: { pages: [page] }, isLoading: false, error: null, hasNextPage: false, fetchNextPage: vi.fn(), isFetchingNextPage: false });
const mut = (over: Record<string, unknown> = {}) => ({ mutate: vi.fn(), reset: vi.fn(), isPending: false, error: null, ...over });

beforeEach(() => {
  vi.clearAllMocks();
  hooks.useEvalSuites.mockReturnValue(ok(fx.suites()));
  hooks.useEvalSuite.mockReturnValue(ok(fx.suite()));
  hooks.useEvalSuiteCases.mockReturnValue(pages(fx.casePage()));
  hooks.useEvalRuns.mockReturnValue(pages(fx.runs()));
  hooks.useEvalRun.mockReturnValue(ok(fx.failedRun()));
  hooks.useEvalResults.mockReturnValue(pages(fx.results()));
  hooks.useEvalCompare.mockReturnValue(ok(fx.compare()));
  hooks.useVoidEvalRun.mockReturnValue(mut());
  hooks.useArtifactFamilies.mockReturnValue(ok(fx.families()));
  hooks.useArtifacts.mockReturnValue(pages(fx.artifacts()));
  hooks.useArtifact.mockReturnValue(ok(fx.artifact()));
  hooks.useArtifactLineage.mockReturnValue(ok(fx.lineage()));
  hooks.usePromotionCheck.mockReturnValue(ok(fx.promotionBlocked()));
  hooks.usePromoteArtifact.mockReturnValue(mut());
  hooks.useDeprecateArtifact.mockReturnValue(mut());
  hooks.useFamilyRequirements.mockReturnValue(ok(fx.requirements()));
  hooks.useSaveFamilyRequirements.mockReturnValue(mut());
  hooks.useFamilyEvals.mockReturnValue(ok(fx.matrix()));
  hooks.useSuiteNames.mockReturnValue(new Map(fx.suites().suites.map((s) => [s.lineage_id, s])));
});

const at = (query: string, ui: React.ReactNode) => render(<MemoryRouter initialEntries={[`/projects/p?${query}`]}>{ui}</MemoryRouter>);

describe('helpers', () => {
  it('gates writes on Team and Enterprise only', () => {
    expect(planAllowsEvals('team')).toBe(true);
    expect(planAllowsEvals('enterprise')).toBe(true);
    expect(planAllowsEvals('pro')).toBe(false);
    expect(planAllowsEvals(null)).toBe(false);
  });
  it('links only https link_url values', () => {
    expect(safeArtifactLink({ link_url: 'https://huggingface.co/x' })).toBe('https://huggingface.co/x');
    expect(safeArtifactLink({ link_url: null })).toBeNull();
    expect(safeArtifactLink({ link_url: 'javascript:alert(1)' })).toBeNull();
    expect(safeArtifactLink({ link_url: 'http://example.test' })).toBeNull();
  });
  it('lays lineage parents left of children', () => {
    const l = layoutSteps(lineageNodes(fx.lineage()));
    const col = Object.fromEntries(l.nodes.map((n) => [n.step.id, n.column]));
    expect(col.art_notes_v3).toBe(0);
    expect(col.art_model_v25).toBe(1);
    expect(describeLineage(fx.lineage())).toContain('note-summarizer@2.5.0 trained on deid-notes@v3');
  });
});

describe('Evals runs', () => {
  it('lists runs with verdict and trust badges, and compares two picked runs', async () => {
    const user = userEvent.setup();
    const { container } = at('ev=runs', <EvalsTab projectId="p" tier="team" canAdmin />);
    const table = screen.getByRole('region', { name: 'Eval runs' });
    expect(within(table).getAllByText('Trusted')).toHaveLength(1);
    expect(within(table).getByText('Unverified')).toBeInTheDocument();
    expect(within(table).getByText('Running')).toBeInTheDocument();
    const compare = screen.getByRole('button', { name: 'Compare' });
    expect(compare).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: 'Pick evr_failed002 to compare' }));
    await user.click(screen.getByRole('checkbox', { name: 'Pick evr_trusted01 to compare' }));
    expect(compare).toBeEnabled();
    expect(await axe(container)).toHaveNoViolations();
    await user.click(compare);
    expect(await screen.findByTestId('eval-compare')).toBeInTheDocument();
    expect(hooks.useEvalCompare).toHaveBeenLastCalledWith('p', 'evr_failed002', 'evr_trusted01');
  });

  it('shows an unverified run with its reason, regressions and results', async () => {
    const { container } = at('ev=runs&evrun=evr_failed002', <EvalsTab projectId="p" tier="team" canAdmin={false} />);
    expect(screen.getByText('Unverified run')).toBeInTheDocument();
    expect(screen.getAllByText(/minted by the person who registered/).length).toBeGreaterThan(0);
    expect(screen.getByText('Why this verdict')).toBeInTheDocument();
    expect(screen.getByText(/cleared by the retention window/)).toBeInTheDocument();
    // Void is for project admins only.
    expect(screen.queryByRole('button', { name: 'Void run' })).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('voids a run only with a reason', async () => {
    const user = userEvent.setup();
    const m = mut();
    hooks.useVoidEvalRun.mockReturnValue(m);
    at('ev=runs&evrun=evr_failed002', <EvalsTab projectId="p" tier="pro" canAdmin />);
    await user.click(screen.getByRole('button', { name: 'Void run' }));
    const dialog = screen.getByRole('dialog', { name: 'Void this run?' });
    await user.click(within(dialog).getByRole('button', { name: 'Void run' }));
    expect(m.mutate).not.toHaveBeenCalled();
    expect(within(dialog).getByText('Give a reason for voiding the run.')).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText('Reason'), 'Wrong model build');
    await user.click(within(dialog).getByRole('button', { name: 'Void run' }));
    expect(m.mutate).toHaveBeenCalledWith({ runId: 'evr_failed002', reason: 'Wrong model build' }, expect.anything());
  });

  it('calls out an untrusted side first in compare', () => {
    at('ev=compare&a=evr_trusted01&b=evr_failed002', <EvalsTab projectId="p" tier="team" canAdmin={false} />);
    expect(screen.getByText('Read this first')).toBeInTheDocument();
    expect(screen.getByText(/does not count toward any gate/)).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Score change per case' })).toBeInTheDocument();
  });

  it('shows the plan notice off Team', () => {
    at('ev=runs', <EvalsTab projectId="p" tier="free" canAdmin={false} />);
    expect(screen.getByText('Evaluations are a Team feature')).toBeInTheDocument();
  });
});

describe('Evals suites', () => {
  it('shows inline case text behind a disclosure', async () => {
    at('ev=suites&suite=evs_clinical0002', <EvalsTab projectId="p" tier="team" canAdmin={false} />);
    expect(screen.getByText('Clinical note summaries')).toBeInTheDocument();
    expect(screen.getByText('Pass rate at least 90%')).toBeInTheDocument();
    expect(screen.getByText('Patient on warfarin 5 mg daily, INR 2.4.')).toBeInTheDocument();
  });

  it('shows hashes only for a reference-storage suite', async () => {
    hooks.useEvalSuite.mockReturnValue(ok(fx.referenceSuite()));
    hooks.useEvalSuiteCases.mockReturnValue(pages(fx.referenceCasePage()));
    const { container } = at('ev=suites&suite=evs_phi00000001', <EvalsTab projectId="p" tier="team" canAdmin={false} />);
    expect(screen.getByText(/case text stays with you/)).toBeInTheDocument();
    expect(screen.getAllByText('sha256')).toHaveLength(2);
    expect(screen.queryByText('Input')).not.toBeInTheDocument();
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe('Artifacts', () => {
  it('links https URIs and leaves the rest copy-only', () => {
    const { rerender } = render(<ArtifactUri artifact={fx.dataset()} />);
    expect(screen.getByRole('link', { name: /Open/ })).toHaveAttribute('href', 'https://huggingface.co/datasets/acme/deid-notes');
    rerender(<ArtifactUri artifact={fx.artifact()} />);
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByText('Copy only')).toBeInTheDocument();
  });

  it('shows the artifact with lineage and the blocked promotion', async () => {
    const { container } = at('art=art_model_v25', <ArtifactsTab projectId="p" tier="team" canEdit canAdmin onOpenRun={vi.fn()} />);
    expect(screen.getByRole('heading', { name: 'note-summarizer@2.5.0' })).toBeInTheDocument();
    expect(screen.getByTestId('lineage-graph')).toBeInTheDocument();
    expect(screen.getByText('Blocked from Staging')).toBeInTheDocument();
    expect(screen.getByText(/clinical-notes has no trusted passed run/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Promote to Staging' })).toBeDisabled();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('opens a lineage neighbour from the link list', async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(<LineageGraph lineage={fx.lineage()} onOpen={onOpen} />);
    await user.click(screen.getByRole('button', { name: 'deid-notes@v3' }));
    expect(onOpen).toHaveBeenCalledWith('art_notes_v3');
  });

  it('promotes after confirming when the gate allows it', async () => {
    const user = userEvent.setup();
    hooks.usePromotionCheck.mockReturnValue(ok(fx.promotionAllowed('staging')));
    const m = mut();
    hooks.usePromoteArtifact.mockReturnValue(m);
    render(<PromotionPanel projectId="p" artifact={fx.artifact()} canPromote />);
    expect(screen.getByText('Ready for Staging')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Promote to Staging' }));
    const dialog = screen.getByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Promote' }));
    expect(m.mutate).toHaveBeenCalledWith({ artifactId: 'art_model_v25', toStage: 'staging' }, expect.anything());
  });

  it('shows the blockers a refused promotion returns', () => {
    hooks.usePromotionCheck.mockReturnValue(ok(fx.promotionAllowed('staging')));
    const err = new ApiError(409, JSON.stringify({ error: { code: '409', message: 'This version cannot be promoted yet.', details: { error: 'promotion_blocked', blockers: [{ code: 'x', message: 'A required suite lost its trusted pass.' }] } } }));
    hooks.usePromoteArtifact.mockReturnValue(mut({ error: err }));
    render(<PromotionPanel projectId="p" artifact={fx.artifact()} canPromote />);
    expect(screen.getByText('Promotion refused')).toBeInTheDocument();
    expect(screen.getByText('A required suite lost its trusted pass.')).toBeInTheDocument();
  });

  it('hides Promote and Deprecate from readers', () => {
    at('art=art_model_v25', <ArtifactsTab projectId="p" tier="team" canEdit={false} canAdmin={false} onOpenRun={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Promote to Staging' })).not.toBeInTheDocument();
    expect(screen.getByText(/Promoting needs project edit access/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Deprecate' })).not.toBeInTheDocument();
  });

  it('shows the version × suite matrix and lets an admin edit requirements', async () => {
    const user = userEvent.setup();
    const onOpenRun = vi.fn();
    const save = mut();
    hooks.useSaveFamilyRequirements.mockReturnValue(save);
    const { container } = at('family=model:note-summarizer', <ArtifactsTab projectId="p" tier="team" canEdit canAdmin onOpenRun={onOpenRun} />);
    expect(screen.getByRole('region', { name: 'Versions by suite' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Clinical note summaries on 2.4.1: passed/ }));
    expect(onOpenRun).toHaveBeenCalledWith('evr_trusted01');
    expect(await axe(container)).toHaveNoViolations();
    const saveBtn = screen.getByRole('button', { name: 'Save requirements' });
    expect(saveBtn).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: 'PHI redaction (reference)' }));
    await user.click(saveBtn);
    expect(save.mutate).toHaveBeenCalledWith(
      { kind: 'model', name: 'note-summarizer', suites: ['evl_clinical', 'evl_phi'], require_tags: { phi_compliant: 'v3' } },
      expect.anything(),
    );
  });

  it('shows requirements read-only to non-admins', () => {
    at('family=model:note-summarizer', <ArtifactsTab projectId="p" tier="team" canEdit canAdmin={false} onOpenRun={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Save requirements' })).not.toBeInTheDocument();
    expect(screen.getByText(/Training data must carry phi_compliant=v3/)).toBeInTheDocument();
  });
});
