export function renderSnippet(html: string): string {
  // Server returns <mark>term</mark> in snippets
  // Sanitize everything EXCEPT <mark> tags
  return html
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/&lt;mark&gt;/g, '<mark>')
    .replace(/&lt;\/mark&gt;/g, '</mark>');
}
