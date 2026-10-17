"""start_smoke.py <staging> <new folder>: one real Start-Coop.cmd start of a copy of the 0.0.13 staging, up to the room browser (boot-log).

No room, no game: as soon as the room browser answers, every process from the folder is ended.
"""
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True  # Never leave __pycache__ in the staging this imports from.
STAGE = Path(sys.argv[1])
FOLDER = Path(sys.argv[2])
sys.path.insert(0, str(STAGE / 'reconstruction'))
import self_update as su  # noqa: E402  (processes_in: programs started from the folder)


def log(tag, text):
    print(f'[{tag:^6}] {text}', flush=True)


t0 = time.time()
log(' .... ', f'Kopie {FOLDER}')
for path in STAGE.rglob('*'):
    out = FOLDER / path.relative_to(STAGE)
    if path.is_dir():
        out.mkdir(parents=True, exist_ok=True)
    elif path.stat().st_size > 8 << 20:
        out.parent.mkdir(parents=True, exist_ok=True)
        os.link(path, out)
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)
log('  OK  ', f'Kopie ({time.time() - t0:.1f} s)')

env = {k: v for k, v in os.environ.items() if not k.startswith('COOP_')}  # The real GitHub channel.
started = time.time()
log(' .... ', 'Start-Coop.cmd starten (echter Kanal) bis zum Raumbrowser')
process = subprocess.Popen(f'cmd /c ""{FOLDER / "Start-Coop.cmd"}""', stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           stdin=subprocess.DEVNULL, env=env, cwd=FOLDER)
lines = queue.Queue()
threading.Thread(target=lambda: [lines.put(l.decode('utf-8', 'replace').rstrip()) for l in process.stdout], daemon=True).start()
url, output = None, []
while time.time() - started < 90 and url is None:
    try:
        line = lines.get(timeout=1)
    except queue.Empty:
        if process.poll() is not None:
            break
        continue
    output.append(line)
    log(' INFO ', f'  {line}')
    match = re.search(r'Room browser: (http://127\.0\.0\.1:\d+)', line)
    if match:
        url = match.group(1)
ok = False
if url:
    for _ in range(20):
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                ok = response.status == 200
                break
        except OSError:
            time.sleep(.5)
    log('  OK  ' if ok else 'FAILED', f'Raumbrowser {url} antwortet ({time.time() - started:.1f} s nach dem Start)')
else:
    log('FAILED', 'Raumbrowser nicht erreicht')

log(' .... ', 'Launcher beenden')
running = su.processes_in(FOLDER)
for entry in running:
    pid = int(re.search(r'process (\d+)', entry).group(1))
    subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True)
subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
time.sleep(2)
left = su.processes_in(FOLDER)
log('  OK  ' if not left else 'FAILED', f'Launcher beenden (beendet: {running}; übrig: {left or "keine"})')
update_lines = [l for l in output if 'co-op update' in l.lower() or 'automatic update' in l.lower()]
log(' INFO ', f'Update-Prüfung: {update_lines}')
sys.exit(0 if ok and not left else 1)
