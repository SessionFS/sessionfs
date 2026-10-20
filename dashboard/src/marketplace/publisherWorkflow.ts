/** Minimal GitHub Actions workflow for trusted publishing (no stored API key). */
export function workflowSnippet(namespace: string): string {
  return `name: Publish skill
on:
  push:
    tags: ['v*']
permissions:
  contents: read
jobs:
  publish:
    runs-on: ubuntu-latest
    environment: release
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - run: pipx install sessionfs
      - run: sfs skill publish . --trusted-publishing --namespace ${namespace}`;
}
