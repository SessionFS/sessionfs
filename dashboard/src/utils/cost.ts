const PRICING: Record<string, { input: number; output: number }> = {
  'claude-opus-4-6': { input: 15, output: 75 },
  'claude-sonnet-4-6': { input: 3, output: 15 },
  'claude-haiku-4-5': { input: 0.8, output: 4 },
  'gpt-4o': { input: 2.5, output: 10 },
};

export function estimateCost(
  modelId: string | null | undefined,
  inputTokens: number,
  outputTokens: number,
): number {
  if (!modelId) return 0;
  let rate = PRICING[modelId];
  if (!rate) {
    for (const [prefix, r] of Object.entries(PRICING)) {
      if (modelId.startsWith(prefix)) { rate = r; break; }
    }
  }
  if (!rate) return 0;
  return (inputTokens / 1_000_000) * rate.input + (outputTokens / 1_000_000) * rate.output;
}
