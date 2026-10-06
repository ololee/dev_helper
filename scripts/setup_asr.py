#!/usr/bin/env python3
"""Explicit optional Apple Silicon Whisper setup; never runs on recording or sync."""
import argparse
import json
import hashlib
from pathlib import Path
import platform
import subprocess
import urllib.request
import venv


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--checkout', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--model', default='mlx-community/whisper-small-mlx')
    parser.add_argument('--configure', metavar='DESKTOP_URL', help='Configure this running assistant after installation')
    args = parser.parse_args()
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise SystemExit('This optional MLX setup requires an Apple Silicon Mac. Cloud ASR and whisper.cpp can be configured separately.')
    root = args.checkout.expanduser().resolve(strict=True)
    if not (root / 'server.py').is_file() or not (root / 'pyproject.toml').is_file():
        raise SystemExit('Choose an existing DevHelper desktop installation')
    runtime = root / '.asr-venv'
    model = root / 'data/models' / ('whisper-' + hashlib.sha256(args.model.encode()).hexdigest()[:16])
    if not runtime.exists():
        venv.EnvBuilder(with_pip=True).create(runtime)
    python = runtime / 'bin/python'
    subprocess.run([str(python), '-m', 'ensurepip', '--upgrade'], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', 'mlx-whisper', 'imageio-ffmpeg'], check=True)
    install = """import sys
from pathlib import Path
from huggingface_hub import snapshot_download
import imageio_ffmpeg
shim=Path(sys.executable).parent/'ffmpeg'
if not shim.exists(): shim.symlink_to(imageio_ffmpeg.get_ffmpeg_exe())
snapshot_download(sys.argv[1],local_dir=sys.argv[2],allow_patterns=['config.json','*.safetensors','*.npz'],token=False)
"""
    subprocess.run([str(python), '-c', install, args.model, str(model)], check=True)
    config = dict(asrBackend='local', localBackend='mlx', localPython=str(python), localModel=str(model))
    if args.configure:
        base = args.configure.rstrip('/')
        with urllib.request.urlopen(base + '/health', timeout=10) as response:
            if json.load(response).get('appId') != 'devhelper-desktop':
                raise SystemExit('The chosen address is not a DevHelper desktop assistant')
        request = urllib.request.Request(base + '/api/workflows/config', data=json.dumps(config).encode(), headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=10) as response:
            if not json.load(response).get('asrConfigured'):
                raise SystemExit('ASR was installed but configuration was not confirmed')
    print(json.dumps({'installed': True, 'configured': bool(args.configure), 'settings': config}))


if __name__ == '__main__':
    main()
