// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  site: 'https://sessionfs.dev',
  integrations: [
    starlight({
      title: 'SessionFS',
      logo: {
        light: './src/assets/logo.svg',
        dark: './src/assets/logo.svg',
      },
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/SessionFS/sessionfs' },
      ],
      sidebar: [
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
            { label: 'LLM Judge', slug: 'judge' },
            { label: 'Team Handoff', slug: 'handoff' },
            { label: 'Session Summary', slug: 'summary' },
            { label: 'MCP Server', slug: 'mcp' },
            { label: 'Git Integration', slug: 'git-integration' },
            { label: 'Project Context', slug: 'project-context' },
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
