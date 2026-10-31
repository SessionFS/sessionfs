import type { LoopChild } from '../../api/reviewLoop';
import { formatTokens, formatUsd } from './format';

export interface Stage {
  key: string;
  label: string;
  /** Review stages carry their round's harmful-finding count. */
  round?: { round: number; harmful: number };
}

const MERGED_STATUSES = ['done', 'merged', 'resolved', 'closed'];

/** Stages for one lane: Brief, Build, Review R1, Fix, R2 ..., Clean, Ready, Merged. */
export function laneStages(child: LoopChild): { stages: Stage[]; current: number } {
  const rounds = child.rounds ?? [];
  const stages: Stage[] = [
    { key: 'brief', label: 'Brief' },
    { key: 'build', label: 'Build' },
  ];
  rounds.forEach((r, i) => {
    stages.push({ key: `review-${r.round}`, label: `Review R${r.round}`, round: r });
    if (r.harmful > 0 && (i < rounds.length - 1 || child.harmful_open > 0)) {
      stages.push({ key: `fix-${r.round}`, label: 'Fix' });
    }
  });
  stages.push({ key: 'clean', label: 'Clean' }, { key: 'ready', label: 'Ready' }, { key: 'merged', label: 'Merged' });

  const at = (key: string) => stages.findIndex((s) => s.key === key);
  let current: number;
  const last = rounds[rounds.length - 1];
  if (MERGED_STATUSES.includes(child.status)) current = at('merged');
  else if (child.ready) current = at('ready');
  else if (last) {
    current = last.harmful > 0 || child.harmful_open > 0 ? at(`fix-${last.round}`) : at('clean');
    if (current < 0) current = at(`review-${last.round}`);
  } else current = child.status === 'in_progress' ? at('build') : at('brief');
  return { stages, current };
}

/** Last three rounds each at least as harmful as the one before, and not clean. */
export function designProblem(child: LoopChild): boolean {
  const r = (child.rounds ?? []).slice(-3);
  return r.length === 3 && r[0].harmful > 0 && r[1].harmful >= r[0].harmful && r[2].harmful >= r[1].harmful;
}

/** A child that is being worked on right now. */
export function isRunning(child: LoopChild): boolean {
  return child.status === 'in_progress' && !child.needs_human && !child.ready;
}

/** Rounds still expected after a round that found harm: the fix, then a confirming review. */
export const ROUNDS_AHEAD = 2;

/**
 * "At this rate, 2 more rounds ≈ 340k tokens; the budget allows 1." Null when
 * there is nothing to forecast. Dollars only when the response carried cost_usd.
 */
export function forecast(child: LoopChild): string | null {
  const last = (child.rounds ?? [])[(child.rounds ?? []).length - 1];
  if (!last || child.round < 1 || child.tokens <= 0 || child.ready) return null;
  if (last.harmful === 0 && child.harmful_open === 0) return null;
  const perRound = child.tokens / child.round;
  let text = `At this rate, ${ROUNDS_AHEAD} more rounds ≈ ${formatTokens(Math.round(perRound * ROUNDS_AHEAD))} tokens`;
  if (child.cost_usd !== undefined) text += ` (${formatUsd((child.cost_usd / child.round) * ROUNDS_AHEAD)})`;
  const caps: number[] = [];
  if (child.budget.tokens) caps.push(Math.max(0, Math.floor((child.budget.tokens - child.tokens) / perRound)));
  if (child.budget.max_rounds) caps.push(Math.max(0, child.budget.max_rounds - child.round));
  if (caps.length) text += `; the budget allows ${Math.min(...caps)}`;
  return `${text}.`;
}
