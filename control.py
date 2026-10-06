#!/usr/bin/env python3
"""Start, inspect or stop only this checkout's DevHelper desktop process."""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / '.venv' / 'bin' / 'python'
SERVER = ROOT / 'server.py'
STATE = ROOT / 'control-state.json'
LOG = ROOT / 'server.log'
APP_ID = 'devhelper-desktop'
DEFAULT_PORT = 8876


class ControlError(RuntimeError):
    pass


def authorized_command(argv: list[str], command: str) -> bool:
    """Allow Python.framework's executable re-exec on macOS.

    Every server argument must still match the owned launch, and the executable
    must be this environment's interpreter or its own base runtime.
    """
    suffix = ' ' + ' '.join(argv[1:])
    if not command.endswith(suffix):
        return False
    executable = command[:-len(suffix)]
    candidates = {str(PYTHON), str(PYTHON.resolve()), str(getattr(sys, '_base_executable', sys.executable))}
    framework_runtime = Path(sys.base_exec_prefix) / 'Resources/Python.app/Contents/MacOS/Python'
    if framework_runtime.is_file():
        candidates.add(str(framework_runtime))
    return executable in candidates


def state_identity(state: dict) -> tuple[str, str]:
    return state['processStarted'], state.get('processCommand', ' '.join(state['argv']))


def read_state(path: Path = STATE) -> dict | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        pid = value['pid']
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            raise ValueError('invalid PID')
        argv = value['argv']
        if not isinstance(argv, list) or argv[:3] != [str(PYTHON), '-u', str(SERVER)] or any(not isinstance(v, str) for v in argv):
            raise ValueError('unexpected executable')
        port = value['port']
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError('invalid port')
        if not isinstance(value['processStarted'], str) or not value['processStarted']:
            raise ValueError('missing process start time')
        command = value.get('processCommand', ' '.join(argv))
        if not isinstance(command, str) or not authorized_command(argv, command):
            raise ValueError('unexpected running executable or arguments')
        return value
    except (OSError, ValueError, KeyError, TypeError) as failed:
        raise ControlError(f'本服务的进程记录无效，未操作任何进程：{path}（{failed}）') from None


def process_identity(pid: int) -> tuple[str, str] | None:
    """ps is read-only; command plus start time rejects recycled PIDs."""
    try:
        started = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'lstart='], text=True, timeout=3).strip()
        command = subprocess.check_output(['/bin/ps', '-ww', '-p', str(pid), '-o', 'command='], text=True, timeout=3).strip()
        return (started, command) if started and command else None
    except subprocess.CalledProcessError:
        return None
    except (OSError, subprocess.TimeoutExpired) as failed:
        raise ControlError(f'无法核实进程身份，未操作任何进程：{failed}') from None


def matches_process(state: dict) -> bool:
    identity = process_identity(state['pid'])
    return identity == state_identity(state)


def health(port: int, host: str = '0.0.0.0') -> dict | None:
    # Never use proxy environment variables for a local health check.
    if host == '::':
        address = '[::1]'
    elif host in ('0.0.0.0', 'localhost', '127.0.0.1'):
        address = '127.0.0.1'
    else:
        try:
            parsed = ipaddress.ip_address(host)
        except ValueError:
            return None
        address = '[' + str(parsed) + ']' if parsed.version == 6 else str(parsed)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f'http://{address}:{port}/health', timeout=1) as response:
            body = response.read(64 * 1024 + 1)
            if len(body) > 64 * 1024:
                return None
            value = json.loads(body)
            if not isinstance(value, dict) or value.get('appId') != APP_ID:
                return None
            return value
    except (OSError, urllib.error.URLError, ValueError):
        return None


def status(state_path: Path = STATE, port: int = DEFAULT_PORT, host: str = '0.0.0.0') -> dict:
    state = read_state(state_path)
    if state:
        current = health(state['port'], state.get('host', '0.0.0.0'))
        identity = process_identity(state['pid'])
        if not identity:
            return {'state': 'stopped', 'managed': True, 'staleRecord': True, 'port': state['port'], 'pid': state['pid']}
        if identity != state_identity(state):
            return {'state': 'identity_mismatch', 'managed': False, 'port': state['port'], 'error': '进程身份已变化；不会停止该进程。'}
        if current and current.get('pid') != state['pid']:
            return {'state': 'identity_mismatch', 'managed': False, 'port': state['port'], 'error': '端口对应另一个进程；不会停止其他程序。'}
        return {**(current or {}), 'state': 'running' if current else 'starting_or_unavailable', 'managed': True,
                'pid': state['pid'], 'port': state['port'], 'logPath': str(LOG), 'dataDir': state.get('dataDir'),
                'managementUrl': f"http://127.0.0.1:{state['port']}/"}
    current = health(port, host)
    if current:
        return {**current, 'state': 'running', 'managed': False,
                'note': '服务由其他入口启动；本脚本没有它的进程记录，不会停止该进程。'}
    return {'state': 'stopped', 'managed': False, 'port': port}


def write_state(value: dict, path: Path = STATE) -> None:
    temporary = path.with_name('.' + path.name + '.' + str(os.getpid()))
    try:
        with temporary.open('w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def start(args: argparse.Namespace) -> dict:
    current = status(port=args.port, host=args.host)
    if current['state'] == 'identity_mismatch':
        raise ControlError(current['error'])
    if current['state'] in ('running', 'starting_or_unavailable'):
        return current
    if not PYTHON.is_file():
        raise ControlError(f'找不到 Python 环境：{PYTHON}。请先按 README 安装依赖。')
    if not SERVER.is_file():
        raise ControlError(f'找不到服务入口：{SERVER}')
    data_dir = Path(args.data_dir).expanduser().resolve()
    shared_dir = Path(args.shared_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    shared_dir.mkdir(parents=True, exist_ok=True)
    argv = [str(PYTHON), '-u', str(SERVER), '--host', args.host, '--port', str(args.port),
            '--data-dir', str(data_dir), '--shared-dir', str(shared_dir)]
    with LOG.open('ab') as log:
        process = subprocess.Popen(argv, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    identity = None
    for _ in range(10):
        identity = process_identity(process.pid)
        if identity or process.poll() is not None:
            break
        time.sleep(0.05)
    if not identity or not authorized_command(argv, identity[1]):
        # This Popen object owns exactly the child created above. Do not leave
        # an unmanageable service behind if identity registration fails.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        raise ControlError(f'未能建立可靠的进程记录，请查看日志：{LOG}')
    state = {'appId': APP_ID, 'pid': process.pid, 'argv': argv, 'processStarted': identity[0], 'processCommand': identity[1],
             'host': args.host, 'port': args.port, 'dataDir': str(data_dir), 'sharedDir': str(shared_dir)}
    write_state(state)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = health(args.port, args.host)
        if current and current.get('pid') == process.pid:
            return status()
        if process.poll() is not None:
            # Remove only the record we just wrote; a competing controller may
            # have started a replacement in the meantime.
            recorded = read_state()
            if recorded and recorded['pid'] == process.pid:
                STATE.unlink(missing_ok=True)
            raise ControlError(f'启动失败，请查看日志：{LOG}')
        time.sleep(0.2)
    raise ControlError(f'启动检查超时；进程记录已保留，请查看日志或运行 status：{LOG}')


def stop() -> dict:
    state = read_state()
    if state is None:
        return {'state': 'stopped_or_unmanaged', 'note': '没有本脚本启动的进程记录，未停止任何其他程序。'}
    identity = process_identity(state['pid'])
    if identity is None:
        STATE.unlink(missing_ok=True)
        return {'state': 'stopped', 'pid': state['pid']}
    if identity != state_identity(state):
        raise ControlError('进程身份不匹配，未停止其他程序。')
    current = health(state['port'], state.get('host', '0.0.0.0'))
    if current and current.get('pid') != state['pid']:
        raise ControlError('端口对应其他进程，未停止任何程序。')
    # Repeat identity immediately before signaling to reduce a stale-PID race.
    if not matches_process(state):
        raise ControlError('进程身份已变化，未停止其他程序。')
    try:
        os.kill(state['pid'], signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        identity = process_identity(state['pid'])
        if identity is None or identity != state_identity(state):
            recorded = read_state()
            if recorded and recorded['pid'] == state['pid']:
                STATE.unlink(missing_ok=True)
            return {'state': 'stopped', 'pid': state['pid']}
        time.sleep(0.2)
    return {'state': 'stopping', 'pid': state['pid'], 'note': '已请求停止，正在等待当前任务完成；进程记录已保留。'}


def main() -> int:
    parser = argparse.ArgumentParser(description='DevHelper 桌面助手启动和停止控制')
    parser.add_argument('action', choices=('start', 'stop', 'status'))
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--data-dir', default=str(ROOT / 'data'))
    parser.add_argument('--shared-dir', default=str(ROOT / 'shared'))
    parser.add_argument('--open-browser', action='store_true', help='启动后打开管理页面')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port 必须是 1 到 65535')
    try:
        if args.action == 'status':
            result = status(port=args.port, host=args.host)
        else:
            # Serialize mutating controller actions so two launches cannot
            # overwrite each other's PID record.
            with (ROOT / 'control.lock').open('a') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                result = start(args) if args.action == 'start' else stop()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.open_browser and args.action == 'start' and result.get('state') == 'running':
            webbrowser.open(result.get('managementUrl') or f"http://127.0.0.1:{result.get('port', args.port)}/")
        return 0
    except ControlError as failed:
        print(str(failed), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
