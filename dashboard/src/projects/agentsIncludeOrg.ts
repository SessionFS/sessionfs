/** Per-project memory of the "Include org people" toggle (GitHub #126). */
export function includeOrgStorageKey(projectId: string): string {
  return `sfs:agents-include-org:${projectId}`;
}

export function readIncludeOrg(projectId: string): boolean {
  try {
    return window.localStorage.getItem(includeOrgStorageKey(projectId)) === '1';
  } catch {
    return false;
  }
}

export function writeIncludeOrg(projectId: string, on: boolean): void {
  try {
    if (on) window.localStorage.setItem(includeOrgStorageKey(projectId), '1');
    else window.localStorage.removeItem(includeOrgStorageKey(projectId));
  } catch {
    // Storage blocked (private window, previews): the toggle still works for this visit.
  }
}
