from pathlib import Path
import ast
import subprocess
import sys

ROOT = Path(r"C:\Users\Public\SMC_Trading_App")
FILES = ["mt5_session_worker.py", "aurex_backend.py"]

def run_git(*args):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip() or result.stderr.strip()
    except Exception as exc:
        return f"Git check unavailable: {exc}"

def main():
    print("=" * 70)
    print("KLYROTRADER MT5 CONNECTOR - LOCAL AUDIT")
    print("=" * 70)
    print(f"Workspace: {ROOT}")

    if not ROOT.is_dir():
        print("\nERROR: Workspace not found.")
        print("Update ROOT to the directory Codex actually audited.")
        sys.exit(1)

    print("\n--- Git status ---")
    print(run_git("status", "--short"))

    print("\n--- Changed files ---")
    print(run_git("diff", "--stat"))

    print("\n--- Python syntax checks ---")
    for name in FILES:
        path = ROOT / name
        if not path.exists():
            print(f"MISSING: {name}")
            continue

        try:
            ast.parse(path.read_text(encoding="utf-8-sig"))
            print(f"PASS: {name}")
        except SyntaxError as exc:
            print(
                f"FAIL: {name}: line {exc.lineno}, "
                f"{exc.msg}"
            )

    worker = ROOT / "mt5_session_worker.py"
    if worker.exists():
        source = worker.read_text(encoding="utf-8-sig")
        checks = {
            "Atomic replace used": "os.replace" in source,
            "Launch ID fencing mentioned": (
                "launch_id" in source or "launchId" in source
            ),
            "Windows Job Object logic present": (
                "JobObject" in source or "Job Object" in source
            ),
            "Free-port selection still present": (
                "pick_free_port" in source
            ),
            "Startup lifespan migration": (
                "@asynccontextmanager" in source
            ),
        }

        print("\n--- Static indicators (not proof of correctness) ---")
        for label, passed in checks.items():
            print(f"{'FOUND' if passed else 'NOT FOUND'}: {label}")

    print("\n--- Relevant tests ---")
    tests = list(ROOT.rglob("test_*.py"))
    tests = [
        p for p in tests
        if ".venv" not in p.parts
        and "site-packages" not in p.parts
        and "node_modules" not in p.parts
    ]

    if not tests:
        print("No test_*.py files found.")
    else:
        for path in tests[:50]:
            print(path.relative_to(ROOT))
        if len(tests) > 50:
            print(f"... and {len(tests) - 50} more")

    print("\nAudit only. No project files were modified.")

if __name__ == "__main__":
    main()