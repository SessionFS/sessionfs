/** Catalog steps are commands only when they look like one (a slash command
 *  or a lowercase program name followed by arguments); anything else is an
 *  instruction sentence and must not be offered for copying. */
export function isCommand(text: string): boolean {
  return text.startsWith('/') || /^[a-z-]+ /.test(text);
}
