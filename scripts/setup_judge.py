"""Prepare and pin a local compiler image; no downloads happen while judging."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cptrainer.cli import _default_home
from cptrainer.config import Paths

parser = argparse.ArgumentParser()
parser.add_argument("--home", type=Path, default=_default_home())
parser.add_argument("--base-image", default="python:3.12-bookworm", help="可复用本机已有、含 g++ / Python / prlimit 的 Debian 镜像")
args = parser.parse_args()
paths = Paths(args.home.expanduser().resolve())
paths.ensure()
if subprocess.run(["docker", "image", "inspect", args.base_image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
    subprocess.run(["docker", "pull", args.base_image], check=True)
subprocess.run(["docker", "build", "--network=none", "--build-arg", "BASE_IMAGE=" + args.base_image, "-t", "cp-trainer-judge:0.3", "-f", str(Path(__file__).with_name("Dockerfile.judge")), str(Path(__file__).parent)], check=True)
image = subprocess.check_output(["docker", "image", "inspect", "cp-trainer-judge:0.3", "--format", "{{.Id}}"], text=True).strip()
versions = subprocess.check_output(["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=32", "--memory=256m", "--entrypoint=/bin/sh", image,
    "-c", "g++ --version | head -n 1; python3 --version; prlimit --version"], text=True)
if "g++" not in versions or "Python 3" not in versions or "prlimit" not in versions:
    raise SystemExit("镜像缺少编译器或运行时，未启用评测")
settings = paths.home / "judge-runtime.json"
temporary = settings.with_suffix(".part")
temporary.write_text(json.dumps({"image": image, "versions": versions.strip()}, ensure_ascii=False, indent=2), encoding="utf-8")
temporary.replace(settings)
print(versions, "评测环境已就绪。重启 CP Trainer 或在评测页面检查环境。")
