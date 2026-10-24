import type { DiffLine } from './types';

export function ellipsis(text: string, maxLen: number): string {
  if (text.length <= maxLen) return text;
  return text.slice(0, maxLen) + '…';
}

export function computeLineDiff(oldStr: string, newStr: string): DiffLine[] {
  const oldLines = oldStr.split('\n');
  const newLines = newStr.split('\n');
  const m = oldLines.length;
  const n = newLines.length;

  // Guard: if either is huge, fall back to simple split view
  if (m * n > 250_000) {
    const result: DiffLine[] = [];
    for (const l of oldLines) result.push({ type: 'removed', text: l });
    for (const l of newLines) result.push({ type: 'added', text: l });
    return result;
  }

  // LCS table
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (oldLines[i - 1] === newLines[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
      }
    }
  }

  // Backtrack
  const result: DiffLine[] = [];
  let i = m, j = n;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
      result.push({ type: 'same', text: oldLines[i - 1] });
      i--; j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      result.push({ type: 'added', text: newLines[j - 1] });
      j--;
    } else {
      result.push({ type: 'removed', text: oldLines[i - 1] });
      i--;
    }
  }

  return result.reverse();
}

export const ERROR_PATTERNS = [/^Error/i, /^Traceback/i, /Traceback \(most recent call last\)/];

export function looksLikeError(content: string): boolean {
  return ERROR_PATTERNS.some((re) => re.test(content));
}
