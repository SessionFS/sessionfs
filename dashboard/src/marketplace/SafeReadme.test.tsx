import { render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { MarketplaceReadme } from '../api/client';
import { SafeReadme } from './SafeReadme';
import { packageImagePath, safeLinkHref } from './discoveryUtils';

const { getMarketplaceAsset } = vi.hoisted(() => ({ getMarketplaceAsset: vi.fn() }));

vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ auth: { client: { getMarketplaceAsset } } }),
}));

function readme(content: string, extra: Partial<MarketplaceReadme> = {}): MarketplaceReadme {
  return {
    version_id: 'skv_1', package_id: 'spk_1', source: 'readme', path: 'README.md', content,
    hidden_unicode: [], truncated: false, untrusted: true, image_path_prefix: '', ...extra,
  };
}

beforeEach(() => {
  getMarketplaceAsset.mockReset();
  globalThis.URL.createObjectURL = vi.fn(() => 'blob:mock');
  globalThis.URL.revokeObjectURL = vi.fn();
});

describe('discovery utils', () => {
  it('keeps only safe link schemes', () => {
    expect(safeLinkHref('https://example.com')).toBe('https://example.com');
    expect(safeLinkHref('mailto:a@b.c')).toBe('mailto:a@b.c');
    expect(safeLinkHref('#usage')).toBe('#usage');
    expect(safeLinkHref('javascript:alert(1)')).toBeNull();
    expect(safeLinkHref('data:text/html,x')).toBeNull();
    expect(safeLinkHref('JaVaScRiPt:x')).toBeNull();
    expect(safeLinkHref(undefined)).toBeNull();
  });

  it('allows only package-relative image paths', () => {
    expect(packageImagePath('assets/logo.png')).toBe('assets/logo.png');
    expect(packageImagePath('./docs/a.png')).toBe('docs/a.png');
    expect(packageImagePath('https://tracker.example/p.gif')).toBeNull();
    expect(packageImagePath('//tracker.example/p.gif')).toBeNull();
    expect(packageImagePath('/etc/x.png')).toBeNull();
    expect(packageImagePath('../x.png')).toBeNull();
    expect(packageImagePath('data:image/png;base64,AAAA')).toBeNull();
  });
});

describe('SafeReadme', () => {
  it('never renders raw HTML or remote images, and nofollows links', () => {
    render(<SafeReadme versionId="skv_1" readme={readme(
      '# Title\n\n<img src="https://t.example/p.gif"><iframe src="https://evil.example"></iframe>\n\n'
      + '![pixel](https://t.example/p.gif)\n\n[docs](https://docs.example) [x](javascript:alert(1))',
    )} />);
    const root = screen.getByTestId('safe-readme');
    expect(root.querySelector('iframe')).toBeNull();
    expect(root.querySelector('img')).toBeNull();
    expect(screen.getByText(/remote image not loaded: pixel/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'docs' })).toHaveAttribute('rel', 'noopener noreferrer nofollow');
    expect(screen.getByRole('link', { name: 'docs' })).toHaveAttribute('target', '_blank');
    expect(screen.queryByRole('link', { name: 'x' })).toBeNull();
    expect(getMarketplaceAsset).not.toHaveBeenCalled();
  });

  it('loads package images through the authenticated asset endpoint', async () => {
    getMarketplaceAsset.mockResolvedValue(new Blob(['png']));
    render(<SafeReadme versionId="skv_9" readme={readme('![logo](assets/logo.png)')} />);
    await waitFor(() => expect(screen.getByRole('img', { name: 'logo' })).toHaveAttribute('src', 'blob:mock'));
    expect(getMarketplaceAsset).toHaveBeenCalledWith('skv_9', 'assets/logo.png');
  });

  it('lists hidden characters and notes a SKILL.md fallback', () => {
    render(<SafeReadme versionId="skv_1" readme={readme('a⟦U+202E⟧b', {
      source: 'skill_md', path: 'skills/a/SKILL.md',
      hidden_unicode: [{ line: 1, column: 2, code_point: 'U+202E', kind: 'bidi' }],
    })} />);
    expect(screen.getByRole('alert')).toHaveTextContent('line 1, column 2: U+202E (bidi)');
    expect(screen.getByText(/No README: showing skills\/a\/SKILL.md/)).toBeInTheDocument();
  });

  it('explains an empty README', () => {
    render(<SafeReadme versionId="skv_1" readme={readme('', { source: 'none', path: null })} />);
    expect(screen.getByText(/no README or SKILL.md text/)).toBeInTheDocument();
  });
});
