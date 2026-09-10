"""Build a portable, dependency-free Python/Web archive; never include local data."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import hashlib

root = Path(__file__).resolve().parents[1]
output = root / "dist"
output.mkdir(exist_ok=True)
target = output / "cp-trainer-0.3.0.zip"
files = [p for p in (root / "cptrainer").rglob("*") if p.is_file() and "__pycache__" not in p.parts]
files += [root / name for name in ["README.md", "LICENSE", "pyproject.toml", "CHANGELOG.md", "scripts/install_service.py", "scripts/setup_judge.py", "scripts/Dockerfile.judge"]]
with ZipFile(target, "w", ZIP_DEFLATED) as archive:
    for path in sorted(files):
        archive.write(path, "cp-trainer/" + path.relative_to(root).as_posix())
with ZipFile(target) as archive:
    assert archive.testzip() is None
    assert "cp-trainer/cptrainer/static/vendor/katex/katex.min.js" in archive.namelist()
target.with_suffix(".zip.sha256").write_text(hashlib.sha256(target.read_bytes()).hexdigest() + "  " + target.name + "\n")
print(target)
