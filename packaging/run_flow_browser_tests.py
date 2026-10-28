"""Run JS regressions against a fresh public fixture, never the loaded extension."""
from pathlib import Path
from runpy import run_path
import shutil
import subprocess
import tempfile


def main():
    root=Path(__file__).resolve().parents[1]
    node=shutil.which('node')
    if not node:
        raise RuntimeError('Node.js is required for the Flow browser regression gate.')
    package=run_path(str(root/'packaging/package_flow_extension.py'))
    with tempfile.TemporaryDirectory(prefix='flow-browser-test-') as temporary:
        fixture=Path(temporary)
        for name in package['SOURCE_FILES']:
            target=fixture/name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(package['public_file'](root,name))
        tests=fixture/'tests';tests.mkdir()
        for path in (root/'tests').glob('*.cjs'):
            shutil.copy2(path,tests/path.name)
        builder=package['module']('browser_regression_builder',fixture/'tools/build_flow_bridge.py')
        builder.build(fixture)
        failures=[]
        scripts=sorted(tests.glob('flow_*_test.cjs'))
        if not scripts:
            raise RuntimeError('No Flow browser regressions found.')
        for script in scripts:
            try:
                result=subprocess.run([node,'--require',str(tests/'flow_no_network.cjs'),str(script)],
                    cwd=fixture,check=False,timeout=45)
                if result.returncode:failures.append(script.name)
            except subprocess.TimeoutExpired:
                failures.append(script.name+' (timeout)')
        print(f'Flow browser scripts: {len(scripts)}, failures: {len(failures)}')
        if failures:
            raise RuntimeError('Flow browser regression failed: '+', '.join(failures))


if __name__=='__main__':main()
