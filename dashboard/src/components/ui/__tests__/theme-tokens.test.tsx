import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Badge as ShimBadge } from '../../Badge';
import { Badge } from '..';
import css from '../../../index.css?raw';

function lightBlock(): string {
  const m = css.match(/\n\[data-theme="light"\] \{[^}]*--success:[^}]*\}/);
  if (!m) throw new Error('light semantic block not found');
  return m[0];
}

describe('semantic colour tokens', () => {
  it('defines --success and a Tailwind success colour', () => {
    expect(css).toMatch(/--success: #3ddc84;/);
    expect(css).toMatch(/--color-success: var\(--success\);/);
  });

  it('overrides every text-use semantic colour for the light theme', () => {
    const block = lightBlock();
    for (const t of [
      '--accent', '--success', '--danger', '--warning', '--info',
      '--severity-critical', '--severity-high', '--severity-low',
      '--role-user', '--role-assistant', '--role-tool', '--role-system',
      '--tool-claude', '--tool-codex', '--tool-gemini', '--tool-copilot',
      '--tool-cursor', '--tool-amp', '--tool-cline', '--tool-roo', '--tool-antigravity',
    ]) {
      expect(block).toContain(`${t}:`);
    }
  });

  it('places the light override after the shared :root block', () => {
    expect(css.indexOf('[data-theme="light"] {\n  --accent')).toBeGreaterThan(
      css.indexOf('--severity-low:      #6b7280;'),
    );
  });

  it('routes filled brand text through --text-on-brand', () => {
    expect(css).toMatch(/\.bg-brand\.text-white \{\s*color: var\(--text-on-brand\);/);
  });
});

describe('Badge', () => {
  it('is exported from ui and re-exported by the legacy path', () => {
    expect(ShimBadge).toBe(Badge);
  });

  it('builds tints from tokens, not rgba literals', () => {
    render(<Badge variant="success" label="ok" />);
    const el = screen.getByText('ok');
    expect(el.style.color).toBe('var(--success)');
    expect(el.getAttribute('style')).not.toMatch(/rgba/);
  });

  it('colours tool badges from tool tokens', () => {
    render(<Badge variant="tool" label="claude-code" />);
    expect(screen.getByText('claude-code').style.color).toBe('var(--tool-claude)');
  });
});
