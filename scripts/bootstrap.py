#!/usr/bin/env python3
"""Install a private runtime, optionally cloning an existing working environment."""
import argparse
import os
import platform
import shutil
import subprocess
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--reuse-venv", type=Path)
p.add_argument("--reuse-models", type=Path)
a = p.parse_args()
root = Path(os.environ.get("ALT_TRACK_DUBBING_HOME", Path.home()/".local/share/alt-track-dubbing"))
root.mkdir(parents=True, exist_ok=True)
uv = shutil.which("uv")
if not uv: raise SystemExit("Install uv first: https://docs.astral.sh/uv/getting-started/installation/")
venv = root/".venv"
if not (venv/"bin/python").exists():
    subprocess.run([uv, "venv", "--python", "3.11", str(venv)], check=True)
    if a.reuse_venv:
        old = a.reuse_venv/"lib/python3.11/site-packages"
        new = venv/"lib/python3.11/site-packages"
        if platform.system() == "Darwin":
            subprocess.run(["cp", "-cR", str(old)+"/.", str(new)], check=True)
        else: shutil.copytree(old, new, dirs_exist_ok=True)
    else:
        deps = Path(__file__).parent/"requirements.txt"
        subprocess.run([uv, "pip", "install", "--python", str(venv/"bin/python"), "-r", str(deps)], check=True)
        backend = "mlx-whisper==0.4.3" if platform.system()=="Darwin" and platform.machine()=="arm64" else "faster-whisper>=1.2,<2"
        subprocess.run([uv, "pip", "install", "--python", str(venv/"bin/python"), backend], check=True)
models = root/"models"
if a.reuse_models and not models.exists():
    if platform.system() == "Darwin": subprocess.run(["cp", "-cR", str(a.reuse_models), str(models)], check=True)
    else: shutil.copytree(a.reuse_models, models)
models.mkdir(exist_ok=True)
print(f"Runtime ready: {venv / 'bin/python'}")
print("Model files are downloaded on the first analyze/asr command if not cached.")
