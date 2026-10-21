// Build-time installer for one works-in harness tool (runs in the node stage
// of deploy/harness/Dockerfile, never in the final image).
//
// - refuses a tool that tools.json does not list as included;
// - refuses a package.json whose pin differs from tools.json, or any range;
// - installs with `npm ci` from the committed lockfile (integrity-checked),
//   with install scripts only where tools.json says they are required;
// - removes duplicate platform payloads listed under "prune" (never the
//   directory the tool's bin resolves into);
// - verifies the installed package version, then stages the runtime files
//   under /opt/sfs-harness/rootfs (copied onto / in the final image):
//   the tool's bin link, node itself only when the tool needs it at runtime,
//   and any system-wide config file under tools/<tool>/etc/.
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, realpathSync, rmSync, symlinkSync } from "node:fs";
import path from "node:path";

const tool = process.env.TOOL;
const manifest = JSON.parse(readFileSync("/opt/sfs-harness/tools.json", "utf8"));
const entry = manifest.tools[tool];
if (!tool || !entry) {
  const excluded = manifest.excluded?.[tool];
  console.error(`No harness image for tool ${JSON.stringify(tool)}${excluded ? `: ${excluded}` : ""}`);
  process.exit(1);
}
const workdir = "/opt/sfs-harness/tool";
const pkg = JSON.parse(readFileSync(path.join(workdir, "package.json"), "utf8"));
const pinned = pkg.dependencies?.[entry.npm_package];
if (pinned !== entry.version || Object.keys(pkg.dependencies).length !== 1) {
  console.error(`package.json must depend on exactly ${entry.npm_package}@${entry.version}, found ${JSON.stringify(pkg.dependencies)}`);
  process.exit(1);
}

const args = ["ci", "--omit=dev", "--no-audit", "--no-fund", "--loglevel=warn"];
if (!entry.npm_scripts) args.push("--ignore-scripts");
execFileSync("npm", args, { cwd: workdir, stdio: "inherit" });

const installed = JSON.parse(
  readFileSync(path.join(workdir, "node_modules", entry.npm_package, "package.json"), "utf8"),
);
if (installed.version !== entry.version) {
  console.error(`installed ${entry.npm_package}@${installed.version}, expected ${entry.version}`);
  process.exit(1);
}

const rootfs = "/opt/sfs-harness/rootfs";
const binDir = path.join(rootfs, "usr/local/bin");
mkdirSync(binDir, { recursive: true });
const binLink = path.join(workdir, "node_modules/.bin", entry.binary);
if (!existsSync(binLink)) {
  console.error(`${entry.binary} was not installed`);
  process.exit(1);
}
const binTarget = realpathSync(binLink);
// Duplicate platform payloads (tools.json "prune"); never the bin target.
for (const rel of entry.prune ?? []) {
  const target = path.join(workdir, rel);
  if (!target.startsWith(path.join(workdir, "node_modules") + path.sep)) {
    console.error(`refusing to prune outside node_modules: ${rel}`);
    process.exit(1);
  }
  if (binTarget === target || binTarget.startsWith(target + path.sep)) {
    console.error(`refusing to prune ${rel}: ${entry.binary} resolves into it`);
    process.exit(1);
  }
  rmSync(target, { recursive: true, force: true });
}
// Absolute link into /opt/sfs-harness/tool (identical path in the final image).
symlinkSync(binTarget, path.join(binDir, entry.binary));
if (entry.needs_node_at_runtime) {
  cpSync(process.execPath, path.join(binDir, "node"));
}
const etc = path.join(workdir, "etc");
if (existsSync(etc)) {
  cpSync(etc, path.join(rootfs, "etc"), { recursive: true });
}
console.log(`installed ${entry.npm_package}@${entry.version} for ${tool}`);
