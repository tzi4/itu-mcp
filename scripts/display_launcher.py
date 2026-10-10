"""Own one private display and the existing application launcher."""
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time

DISPLAY_NUMBER = 193
STOP_SECONDS = 20


class DisplayError(RuntimeError):
    pass


def display_environment(home):
    # The existing app launcher loads its own credentials after display startup.
    # No service environment, credential path, secret or inherited FD goes to Xvfb.
    return {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'LANG': 'C.UTF-8'}


def stop_process(process, grace=STOP_SECONDS):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def wait_display(process, stopped, deadline=10):
    socket = Path(f'/tmp/.X11-unix/X{DISPLAY_NUMBER}')
    started = time.monotonic()
    while time.monotonic() - started < deadline:
        if stopped():
            raise DisplayError('Display startup was interrupted.')
        if process.poll() is not None:
            raise DisplayError('Private Xvfb exited during startup.')
        if socket.exists() and stat.S_ISSOCK(socket.stat().st_mode):
            endpoints = [line.split()[-1] for line in Path('/proc/self/net/unix').read_text().splitlines()
                         if line.split()[-1:] in ([str(socket)], ['@' + str(socket)] )]
            if endpoints != [str(socket)]:
                raise DisplayError('Private display transport validation failed.')
            return
        time.sleep(0.05)
    raise DisplayError('Private Xvfb did not become ready.')


def launch_application(launch):
    launch = Path(launch)
    if not launch.is_absolute() or not launch.is_file() or not os.access(launch, os.X_OK):
        raise DisplayError('An absolute executable application launcher is required.')
    # systemd PrivateTmp=yes gives this service a private filesystem socket.
    # Never remove an existing display socket/lock that this process did not own.
    for path in (f'/tmp/.X11-unix/X{DISPLAY_NUMBER}', f'/tmp/.X{DISPLAY_NUMBER}-lock'):
        if Path(path).exists():
            raise DisplayError('The reserved private display is already in use.')
    requested_signal = []

    def request_stop(signum, frame):
        if not requested_signal:
            requested_signal.append(signum)

    previous = {signum: signal.signal(signum, request_stop) for signum in (signal.SIGTERM, signal.SIGINT)}
    display = app = None
    try:
        with tempfile.TemporaryDirectory(prefix='paperbridge-display-') as directory:
            private = Path(directory)
            authority = private / 'Xauthority'
            authority.touch(mode=0o600)
            environment = display_environment(private)
            display_name = f':{DISPLAY_NUMBER}'
            subprocess.run(['/usr/bin/xauth', '-f', str(authority), 'source', '-'],
                input=f'add {display_name} MIT-MAGIC-COOKIE-1 {secrets.token_hex(16)}\n'.encode(),
                env=environment, close_fds=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5, check=True)
            try:
                display = subprocess.Popen(['/usr/bin/Xvfb', display_name, '-screen', '0', '1280x900x24',
                    '-nolisten', 'tcp', '-nolisten', 'local', '-auth', str(authority), '-noreset'],
                    env=environment, close_fds=True, start_new_session=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                wait_display(display, lambda: bool(requested_signal))
                if requested_signal:
                    return 128 + requested_signal[0]
                app_environment = dict(os.environ, DISPLAY=display_name, XAUTHORITY=str(authority))
                app = subprocess.Popen([str(launch)], env=app_environment, close_fds=True,
                                       start_new_session=True, stdin=subprocess.DEVNULL)
                while True:
                    result = app.poll()
                    if result is not None:
                        return result if result >= 0 else 128 - result
                    if requested_signal:
                        return 128 + requested_signal[0]
                    if display.poll() is not None:
                        raise DisplayError('Private Xvfb exited while the application was running.')
                    time.sleep(0.1)
            finally:
                # Give the application a bounded shutdown while its display is
                # available, then reap the display before deleting authorization.
                try:
                    stop_process(app)
                finally:
                    stop_process(display, grace=3)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Usage: display_launcher.py /absolute/app-launch')
    try:
        raise SystemExit(launch_application(sys.argv[1]))
    except (DisplayError, OSError, subprocess.SubprocessError) as exc:
        print('PaperBridge display launcher failed: ' + type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
