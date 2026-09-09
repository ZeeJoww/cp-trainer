"""Install this checkout as a current-user systemd service on Linux."""
from pathlib import Path
import json
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

if not sys.platform.startswith("linux"):
    raise SystemExit("此安装器仅用于 Linux systemd；其他系统请运行 python -m cptrainer.web。")
root = Path(__file__).resolve().parents[1]
folder = Path.home() / ".config" / "systemd" / "user"
folder.mkdir(parents=True, exist_ok=True)
quote = lambda value: json.dumps(str(value).replace("%", "%%"), ensure_ascii=False)
unit = f"""[Unit]
Description=CP Trainer local Web application

[Service]
Type=simple
WorkingDirectory={str(root).replace('%', '%%')}
ExecStart={quote(sys.executable)} -m cptrainer.web
Restart=on-failure
RestartSec=3
UMask=0077

[Install]
WantedBy=default.target
"""
destination = folder / "cp-trainer.service"
if destination.exists() and destination.read_text() != unit:
    destination.with_suffix(".service.bak").write_text(destination.read_text())
destination.write_text(unit)
subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
subprocess.run(["systemctl", "--user", "enable", "cp-trainer.service"], check=True)
subprocess.run(["systemctl", "--user", "restart", "cp-trainer.service"], check=True)
subprocess.run(["systemctl", "--user", "is-active", "--quiet", "cp-trainer.service"], check=True)
for attempt in range(30):
    try:
        with urlopen("http://127.0.0.1:8765/api/state", timeout=1) as response:
            assert response.status == 200
        break
    except (URLError, TimeoutError):
        if attempt == 29:
            raise SystemExit("服务已启动，但 HTTP 检查未通过，请查看 systemctl --user status cp-trainer。")
        time.sleep(0.1)
print("CP Trainer: http://127.0.0.1:8765/")
