"""Run each unittest module in a fresh process to isolate native Qt state.

Several GUI tests create and close multimedia widgets.  Individually they pass,
but sharing one QApplication/process across the whole suite can terminate the
Windows interpreter without a Python traceback.  Process isolation retains the
full test suite while making such failures attributable to one module.
"""

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    modules = sorted(path.stem for path in (project_root / "tests").glob("test_*.py"))
    if not modules:
        print("No test modules found.", file=sys.stderr)
        return 1

    environment = os.environ.copy()
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    failed = []
    for module in modules:
        name = f"tests.{module}"
        print(f"\n=== {name} ===", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", name],
            cwd=project_root,
            env=environment,
            check=False,
        )
        if result.returncode:
            failed.append((name, result.returncode))

    if failed:
        print("\nFailed test modules:", file=sys.stderr)
        for name, code in failed:
            print(f"  {name}: exit {code}", file=sys.stderr)
        return 1

    print(f"\nAll {len(modules)} test modules passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
