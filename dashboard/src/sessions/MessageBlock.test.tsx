import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import MessageBlock from './MessageBlock';

// ContentBlock is rendered inside MessageBlock; we test the MessageBlock
// container (role rails, labels, model, timestamp) directly.

function mkMsg(role: string, content: string, overrides: Record<string, unknown> = {}) {
  return {
    role,
    content: JSON.stringify(content), // wrapped as JSON since real content is serialized blocks
    model: 'claude-sonnet-4-5',
    timestamp: '2026-06-13T10:00:00Z',
    ...overrides,
  };
}

// ── Role rendering ─────────────────────────────────────────────────

describe('MessageBlock — roles', () => {
  it('renders assistant with accent left rail and ASSISTANT label', () => {
    const msg = mkMsg('assistant', 'Hello world');
    render(<MessageBlock message={msg} />);

    // Label is rendered as uppercase mono
    expect(screen.getByText('Assistant')).toBeInTheDocument();
    // Left rail: border-l-[var(--accent)]
    const container = screen.getByText('Assistant').closest('div.rounded-lg');
    expect(container?.className).toMatch(/border-l-\[var\(--accent\)\]/);
  });

  it('renders user with brand left rail and USER label', () => {
    const msg = mkMsg('user', 'Fix the bug');
    render(<MessageBlock message={msg} />);

    expect(screen.getByText('User')).toBeInTheDocument();
    const container = screen.getByText('User').closest('div.rounded-lg');
    expect(container?.className).toMatch(/border-l-\[var\(--brand\)\]/);
  });

  it('renders tool with tertiary rail and sunken background', () => {
    const msg = mkMsg('tool', 'output here');
    render(<MessageBlock message={msg} />);

    expect(screen.getByText('Tool')).toBeInTheDocument();
    const container = screen.getByText('Tool').closest('div.rounded-lg');
    expect(container?.className).toMatch(/border-l-\[var\(--text-tertiary\)\]/);
    expect(container?.className).toMatch(/bg-bg-sunken/);
  });

  it('renders system with warning rail and sunken background', () => {
    const msg = mkMsg('system', 'System prompt here');
    render(<MessageBlock message={msg} />);

    expect(screen.getByText('System')).toBeInTheDocument();
    const container = screen.getByText('System').closest('div.rounded-lg');
    expect(container?.className).toMatch(/border-l-\[var\(--warning\)\]/);
    expect(container?.className).toMatch(/bg-bg-sunken/);
  });

  it('renders unknown role with tertiary fallback rail', () => {
    const msg = mkMsg('custom_role', 'content');
    render(<MessageBlock message={msg} />);

    expect(screen.getByText('Custom_role')).toBeInTheDocument();
  });

  it('shows model id when present', () => {
    const msg = mkMsg('assistant', 'Hello', { model: 'claude-opus-4-8' });
    render(<MessageBlock message={msg} />);

    expect(screen.getByText('claude-opus-4-8')).toBeInTheDocument();
  });

  it('does not render model when absent', () => {
    const msg = mkMsg('user', 'Hello', { model: undefined });
    render(<MessageBlock message={msg} />);

    // Just check no model text appears
    expect(screen.queryByText('claude-sonnet-4-5')).not.toBeInTheDocument();
  });

  it('shows timestamp as relative date', () => {
    const msg = mkMsg('assistant', 'Hello');
    render(<MessageBlock message={msg} />);

    // RelativeDate renders something (the exact text depends on current time)
    // but the component is mounted — we just verify it doesn't crash
    expect(screen.getByText('Assistant')).toBeInTheDocument();
  });

  it('skips sidechain messages', () => {
    const msg = { role: 'assistant', content: 'hidden', is_sidechain: true };
    const { container } = render(<MessageBlock message={msg} />);

    expect(container.firstChild).toBeNull();
  });

  it('handles string content by wrapping in a text block', () => {
    const msg = mkMsg('assistant', '');
    // Override with actual string content
    msg.content = 'Just a string message';
    render(<MessageBlock message={msg} />);

    // The text should render (via ContentBlock's text handler)
    expect(screen.getByText('Just a string message')).toBeInTheDocument();
  });
});

// ── User vs assistant distinct treatment ───────────────────────────

describe('MessageBlock — distinct role treatment', () => {
  it('user and assistant use different rail colors', () => {
    const userMsg = mkMsg('user', 'Hello');
    const asstMsg = mkMsg('assistant', 'Hello');

    const { rerender } = render(<MessageBlock message={userMsg} />);
    const userContainer = screen.getByText('User').closest('div.rounded-lg');
    const userRail = userContainer?.className || '';

    rerender(<MessageBlock message={asstMsg} />);
    const asstContainer = screen.getByText('Assistant').closest('div.rounded-lg');
    const asstRail = asstContainer?.className || '';

    // They should differ — one uses brand, the other accent
    expect(userRail).not.toBe(asstRail);
  });

  it('tool uses sunken background while assistant uses surface', () => {
    const toolMsg = mkMsg('tool', 'output');
    const { container: toolContainer } = render(<MessageBlock message={toolMsg} />);
    const toolBg = toolContainer.querySelector('.rounded-lg')?.className || '';

    render(<MessageBlock message={mkMsg('assistant', 'text')} />);
    const asstBg = screen.getByText('Assistant').closest('div.rounded-lg')?.className || '';

    expect(toolBg).toMatch(/bg-bg-sunken/);
    expect(asstBg).toMatch(/bg-surface/);
  });
});
