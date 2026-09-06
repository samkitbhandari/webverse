"""Start the whole system: backend, watcher and frontend.

    python scripts/dev.py              # backend + frontend
    python scripts/dev.py --api-only   # backend only
    python scripts/dev.py --seed       # reset and rebuild the demo world first

Ctrl-C stops both.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PYTHON = str(VENV_PY if VENV_PY.exists() else sys.executable)
FRONTEND = ROOT / "frontend"


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-only", action="store_true", help="skip the frontend")
    ap.add_argument("--seed", action="store_true", help="reset and reseed the demo world")
    args = ap.parse_args()

    if args.seed:
        print("→ seeding the demo world…")
        subprocess.run([PYTHON, str(ROOT / "scripts" / "run_demo.py")],
                       env=_env(), cwd=ROOT, check=False)

    procs: list[subprocess.Popen] = []
    try:
        print(f"→ backend  http://127.0.0.1:8000  (docs at /docs)")
        procs.append(subprocess.Popen([PYTHON, "-m", "backend.main"],
                                      env=_env(), cwd=ROOT))

        if not args.api_only:
            npm = shutil.which("npm")
            if npm is None:
                print("! npm not found; skipping the frontend", file=sys.stderr)
            elif not (FRONTEND / "node_modules").exists():
                print("! frontend dependencies missing — run:  cd frontend && npm install",
                      file=sys.stderr)
            else:
                time.sleep(1.5)          # let the API bind before the proxy starts
                print("→ frontend http://127.0.0.1:3000")
                procs.append(subprocess.Popen([npm, "run", "dev"],
                                              cwd=FRONTEND, shell=(os.name == "nt")))

        print("\nDrop a file into incoming_files/ and watch the graph react. Ctrl-C to stop.\n")
        while True:
            for p in procs:
                if p.poll() is not None:
                    print(f"! a process exited with code {p.returncode}", file=sys.stderr)
                    return p.returncode or 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n→ shutting down")
        return 0
    finally:
        for p in procs:
            if p.poll() is None:
                try:
                    p.send_signal(signal.SIGTERM)
                except Exception:
                    p.kill()
        for p in procs:
            try:
                p.wait(timeout=6)
            except Exception:
                p.kill()


if __name__ == "__main__":
    raise SystemExit(main())
