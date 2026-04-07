// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  site: 'https://sessionfs.dev',
  integrations: [
    starlight({
      title: 'SessionFS',
      head: [
        {
          tag: 'script',
          content: `(function(){var p=new URLSearchParams(window.location.search);var t=p.get('theme');if(t==='dark'||t==='light'){document.documentElement.setAttribute('data-theme',t);try{localStorage.setItem('starlight-theme',t)}catch(e){}}})();`,
        },
        { tag: 'link', attrs: { rel: 'preconnect', href: 'https://fonts.googleapis.com' } },
        { tag: 'link', attrs: { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: true } },
        {
          tag: 'link',
          attrs: {
            rel: 'stylesheet',
            href: 'https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap',
          },
        },
      ],
      logo: {
        light: './src/assets/logo.svg',
        dark: './src/assets/logo.svg',
      },
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/SessionFS/sessionfs' },
        { icon: 'external', label: 'Back to site', href: '/' },
      ],
      sidebar: [
        {
          label: 'Back to Site',
          items: [
            { label: 'Home', link: '/' },
            { label: 'Features', link: '/features/' },
            { label: 'Pricing', link: '/pricing/' },
            { label: 'Enterprise', link: '/enterprise/' },
          ],
        },
        {
          label: 'Getting Started',
          items: [
            { label: 'Quickstart', slug: 'quickstart' },
            { label: 'Installation', slug: 'installation' },
          ],
        },
        {
          label: 'Core Concepts',
          items: [
            { label: 'CLI Reference', slug: 'cli' },
            { label: '.sfs Format', slug: 'sfs-format' },
            { label: 'Autosync', slug: 'autosync' },
          ],
        },
        {
          label: 'Features',
          items: [
            { label: 'Knowledge Base', slug: 'knowledge-base' },
            { label: 'LLM Judge', slug: 'judge' },
            { label: 'Team Handoff', slug: 'handoff' },
            { label: 'Session Summary', slug: 'summary' },
            { label: 'MCP Server', slug: 'mcp' },
            { label: 'Git Integration', slug: 'git-integration' },
            { label: 'Project Context', slug: 'project-context' },
          ],
        },
        {
          label: 'Platform',
          items: [
            { label: 'Dashboard', slug: 'dashboard' },
            { label: 'Organizations', slug: 'organizations' },
            { label: 'Billing & Tiers', slug: 'billing' },
          ],
        },
        {
          label: 'Deployment',
          items: [
            { label: 'Self-Hosted (Helm)', slug: 'self-hosted' },
            { label: 'Environment Variables', slug: 'environment' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'REST API', slug: 'api' },
            { label: 'Troubleshooting', slug: 'troubleshooting' },
          ],
        },
      ],
      customCss: ['./src/styles/starlight-custom.css'],
    }),
  ],
  vite: {
    plugins: [tailwindcss()],
  },
});
