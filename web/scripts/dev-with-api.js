// `npm run dev` entrypoint: boots the FastAPI dashboard backend alongside
// `next dev` so opening the dashboard never requires a second terminal.
// This only starts the API process — the trading engine (main.py) stays
// off until someone hits "Start bot" in the dashboard.

const { spawn } = require("node:child_process");
const path = require("node:path");
const http = require("node:http");
const fs = require("node:fs");

const ROOT = path.resolve(__dirname, "..", "..");
const API_HEALTH_URL = "http://127.0.0.1:8000/health";

function apiAlreadyRunning() {
  return new Promise((resolve) => {
    const req = http.get(API_HEALTH_URL, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    req.on("error", () => resolve(false));
    req.setTimeout(800, () => {
      req.destroy();
      resolve(false);
    });
  });
}

function resolvePython() {
  const winVenv = path.join(ROOT, ".venv", "Scripts", "python.exe");
  const posixVenv = path.join(ROOT, ".venv", "bin", "python");
  if (fs.existsSync(winVenv)) return winVenv;
  if (fs.existsSync(posixVenv)) return posixVenv;
  return process.platform === "win32" ? "python" : "python3";
}

async function main() {
  const children = [];

  const killAll = () => {
    for (const child of children) {
      if (!child.killed) child.kill();
    }
  };
  process.on("SIGINT", () => { killAll(); process.exit(0); });
  process.on("SIGTERM", () => { killAll(); process.exit(0); });
  process.on("exit", killAll);

  if (await apiAlreadyRunning()) {
    console.log("[dev] dashboard API already running on :8000 — reusing it");
  } else {
    const python = resolvePython();
    console.log(`[dev] starting dashboard API (${python} dashboard_api.py)`);
    const api = spawn(python, ["dashboard_api.py"], { cwd: ROOT, stdio: "inherit" });
    api.on("exit", (code) => {
      if (code !== null && code !== 0) {
        console.error(`[dev] dashboard API exited with code ${code}`);
      }
    });
    children.push(api);
  }

  // Invoke Next's plain JS entrypoint via `node` rather than the .bin/next(.cmd)
  // shim — spawning .cmd shims directly fails with EINVAL on some Windows/Node
  // combinations, especially with spaces in the path.
  const nextBin = path.join(__dirname, "..", "node_modules", "next", "dist", "bin", "next");
  const next = spawn(process.execPath, [nextBin, "dev"], { cwd: path.join(__dirname, ".."), stdio: "inherit" });
  children.push(next);
  next.on("exit", (code) => {
    killAll();
    process.exit(code ?? 0);
  });
}

main();
