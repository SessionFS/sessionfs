/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Community (default) | enterprise. The enterprise layout ships a
  // dashboard/src/ee/ folder that is absent from the public community tree.
  readonly VITE_SFS_EDITION?: string
}

declare const __APP_VERSION__: string
