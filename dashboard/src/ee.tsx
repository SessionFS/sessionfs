import React from 'react';
import { Route } from 'react-router-dom';

// Enterprise Edition dashboard seam.
//
// Community builds never ship a `src/ee/` folder. `import.meta.glob` expands
// to an empty object when `./ee/routes.tsx` is absent, so this file compiles
// in the public tree without referencing a missing module. In an enterprise
// build (`VITE_SFS_EDITION=enterprise`) `src/ee/routes.tsx` exists and its
// default export — which references `EDITION_MARKER` — is bundled and mounted
// under ENTERPRISE_PATH inside the signed-in layout. Every file under src/ee/
// must declare `export const EDITION_MARKER = 'SESSIONFS-ENTERPRISE-ONLY'` and
// reference it from its default export so `check_public_artifacts.py` can
// prove a community bundle never contains Enterprise code.
//
// <Routes> only accepts <Route> and <React.Fragment> children, so the seam is
// a <Route> element (or null), never a component of its own: a custom
// component placed inside <Routes> throws on every render.
type EeRoutesModule = { default: React.ComponentType };

// The edition test is a build-time constant, so a Community build drops the
// glob's dynamic import entirely and emits no Enterprise chunk, even when
// src/ee/ sits in the tree (the private develop checkout).
const eeRouteModules: Record<string, () => Promise<EeRoutesModule>> =
  import.meta.env.VITE_SFS_EDITION === 'enterprise'
    ? import.meta.glob<EeRoutesModule>('./ee/routes.tsx')
    : {};

const loader = eeRouteModules['./ee/routes.tsx'];
const EeRoutes = loader ? React.lazy(loader) : null;

/** Enterprise pages live under this prefix; src/ee/routes.tsx renders nested <Routes>. */
export const ENTERPRISE_PATH = '/enterprise/*';

/** The Enterprise route for the signed-in layout, or null in a Community build. */
export const enterpriseRoute = EeRoutes ? (
  <Route
    path={ENTERPRISE_PATH}
    element={
      <React.Suspense fallback={null}>
        <EeRoutes />
      </React.Suspense>
    }
  />
) : null;
