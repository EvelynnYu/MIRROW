#!/usr/bin/env node
import fs from "node:fs/promises";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const rootDir = process.cwd();
const appData = process.env.APPDATA ?? "";
const result = {
  mpv: await findMpv(),
  neteaseCliInstalled: await exists(path.join(appData, "npm", "node_modules", "neteasecli", "dist", "index.js")),
};
const failures = [];

if (!result.neteaseCliInstalled) failures.push("neteasecli is not installed");
if (!result.mpv.available) failures.push("mpv is not available");

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}

console.log("smoke ok");

async function exists(filePath) {
  try {
    await fs.access(filePath);
    return true;
  } catch {
    return false;
  }
}

async function commandExists(command) {
  const executable = process.platform === "win32" ? "where.exe" : "which";
  try {
    const { stdout } = await execFileAsync(executable, [command], { timeout: 5000 });
    return stdout.trim();
  } catch {
    return "";
  }
}

async function findMpv() {
  const fromPath = await commandExists("mpv");
  if (fromPath) {
    return { available: true, path: fromPath, source: "PATH" };
  }

  const localMpv = process.platform === "win32" ? path.join(rootDir, "mpv.exe") : path.join(rootDir, "mpv");
  if (await exists(localMpv)) {
    return { available: true, path: localMpv, source: "project-local" };
  }

  return { available: false, path: "", source: "missing" };
}
