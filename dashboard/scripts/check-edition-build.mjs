#!/usr/bin/env node
/**
 * Prove the Community/Enterprise dashboard split holds at build time.
 *
 * 1. Build the real tree as Community into a temp outDir and assert the
 *    SESSIONFS-ENTERPRISE-ONLY marker never appears.
 * 2. Create a temporary src/ee/routes.tsx that renders the marker, build with
 *    VITE_SFS_EDITION=enterprise into a second temp outDir, and assert the
 *    marker IS present and that check_public_artifacts.py exits non-zero.
 *
 * The temporary src/ee/ tree and both temp outDirs are removed in a finally.
 * Plain Node, no dependencies. Run via `npm run check:edition` — deliberately
 * not part of the vitest run.
 */
import { execFileSync, spawnSync } from 'node:child_process';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const DASHBOARD = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC_EE = join(DASHBOARD, 'src', 'ee');
const ROUTES_FILE = join(SRC_EE, 'routes.tsx');
const MARKER = 'SESSIONFS-ENTERPRISE-ONLY';
const CHECK_SCRIPT = join(DASHBOARD, '..', '.github', 'scripts', 'check_public_artifacts.py');
const VITE_BIN = join(DASHBOARD, 'node_modules', '.bin', 'vite');

function runViteBuild(outDir, edition) {
  execFileSync(VITE_BIN, ['build', '--outDir', outDir, '--emptyOutDir'], {
    cwd: DASHBOARD,
    env: { ...process.env, VITE_SFS_EDITION: edition },
    stdio: 'inherit',
  });
}

/** Recursively scan every file under ``dir`` for the marker string. */
function containsMarker(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) {
      if (containsMarker(p)) return true;
    } else if (st.isFile()) {
      let text;
      try {
        text = readFileSync(p, 'utf8');
      } catch {
        continue;
      }
      if (text.includes(MARKER)) return true;
    }
  }
  return false;
}

function checkPublicArtifacts(outDir) {
  const res = spawnSync('python3', [CHECK_SCRIPT, outDir], {
    cwd: DASHBOARD,
    encoding: 'utf8',
  });
  if (res.error) throw res.error;
  return res.status ?? 1;
}

// The private develop checkout carries a real src/ee/: use it (it must render
// the marker like every src/ee/ file) and never create, overwrite or delete it.
// The public tree has none, so a temporary one is made and removed.
const HAD_EE = existsSync(SRC_EE);
if (HAD_EE && !readFileSync(ROUTES_FILE, 'utf8').includes(MARKER)) {
  throw new Error(`${ROUTES_FILE} does not reference ${MARKER}`);
}

const communityOut = mkdtempSync(join(tmpdir(), 'sfs-edition-community-'));
const enterpriseOut = mkdtempSync(join(tmpdir(), 'sfs-edition-enterprise-'));
let madeEe = false;
try {
  // 1. Community build of the real (public) tree.
  runViteBuild(communityOut, 'community');
  if (containsMarker(communityOut)) {
    throw new Error(`Community build leaked ${MARKER} into ${communityOut}`);
  }
  console.log(`community build clean: no ${MARKER}`);

  // 2. Enterprise build, with a temporary routes module that renders the
  // marker when the tree has no src/ee/ of its own.
  if (!HAD_EE) {
    mkdirSync(SRC_EE, { recursive: true });
    madeEe = true;
    writeFileSync(
      ROUTES_FILE,
      `export const EDITION_MARKER = '${MARKER}';\n\nexport default function EeRoutes() {\n  return <div>{EDITION_MARKER}</div>;\n}\n`
    );
  }
  runViteBuild(enterpriseOut, 'enterprise');
  if (!containsMarker(enterpriseOut)) {
    throw new Error(`Enterprise build is missing ${MARKER}`);
  }
  console.log(`enterprise build contains ${MARKER}`);

  const status = checkPublicArtifacts(enterpriseOut);
  if (status === 0) {
    throw new Error('check_public_artifacts.py accepted the Enterprise bundle (expected non-zero)');
  }
  console.log(`check_public_artifacts.py rejected the Enterprise bundle (exit ${status})`);

  // 3. With src/ee/ present, a build that doesn't name its edition must fail,
  // so an Enterprise build can't silently fall back to Community.
  const unnamed = spawnSync(VITE_BIN, ['build', '--outDir', enterpriseOut, '--emptyOutDir'], {
    cwd: DASHBOARD,
    env: { ...process.env, VITE_SFS_EDITION: '' },
    encoding: 'utf8',
  });
  if (unnamed.status === 0) {
    throw new Error('a build with src/ee/ present and no edition succeeded (expected a failure)');
  }
  console.log('a build with src/ee/ present and no edition was refused');
} finally {
  if (madeEe) rmSync(SRC_EE, { recursive: true, force: true });
  rmSync(communityOut, { recursive: true, force: true });
  rmSync(enterpriseOut, { recursive: true, force: true });
}
