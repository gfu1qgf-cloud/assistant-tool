"""Run all modules in bounded, isolated processes and retain every real failure."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def test_environment(root=None):
    environment = os.environ.copy()
    environment.setdefault('QT_QPA_PLATFORM', 'offscreen')
    environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
        HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    for name in ('TEMP', 'TMP', 'TMPDIR'):
        if environment.get(name):
            environment[name] = str(Path(environment[name]).resolve())
    if root is not None:
        root = Path(root).resolve()
        ffmpeg = root / 'runtime/ffmpeg/ffmpeg.exe'
        if ffmpeg.is_file():
            environment.setdefault('NODE_FFMPEG', str(ffmpeg))
            environment['PATH'] = str(ffmpeg.parent) + os.pathsep + environment.get('PATH', '')
    return environment


def execute_module(root, module, reports, environment, timeout=600):
    started = time.monotonic()
    log = reports / (module+'.txt')
    with log.open('w', encoding='utf-8') as output:
        try:
            result = subprocess.run([sys.executable, str(root/'packaging/test_worker.py'), module],
                cwd=root, env=environment, stdout=output, stderr=subprocess.STDOUT,
                timeout=timeout, check=False)
            code, timed_out = result.returncode, False
        except subprocess.TimeoutExpired:
            output.write('\nRELEASE_TEST_TIMEOUT: module exceeded bounded deadline\n')
            code, timed_out = 124, True
    return {'module': module, 'exit_code': code, 'timed_out': timed_out,
        'seconds': round(time.monotonic()-started, 2), 'log': str(log)}


def main():
    root = Path(__file__).resolve().parents[1]
    modules = sorted(path.stem for path in (root/'tests').glob('test_*.py'))
    if not modules:
        raise RuntimeError('No tests found')
    reports = Path(os.environ.get('LZX_TEST_REPORT_DIR', root/'test-results')).resolve()
    reports.mkdir(parents=True, exist_ok=True)
    environment = test_environment(root)
    workers = min(3, max(1, os.cpu_count() or 1))
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(execute_module, root, module, reports, environment): module for module in modules}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            print(f"[{len(results)}/{len(modules)}] {result['module']}: exit {result['exit_code']} "
                f"({result['seconds']}s)", flush=True)
            if result['exit_code']:
                print(Path(result['log']).read_text('utf-8', errors='replace')[-16000:], flush=True)
    results.sort(key=lambda row: row['module'])
    (reports/'summary.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    failed = [row for row in results if row['exit_code']]
    print(f"Release modules: {len(results)}, failed: {len(failed)}", flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
