"""Build the Lambda dependency layer zip (build/layer.zip).

Installs the runtime requirements as manylinux wheels for the Lambda platform
(python3.13, x86_64) — building on Windows/macOS still yields Linux-correct
binaries because everything is fetched with --only-binary.

boto3/botocore are excluded: the Lambda runtime provides them, and they'd eat
~80MB of the 250MB unzipped layer budget for nothing.

Usage:
    python scripts/build_lambda_layer.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO / "requirements.txt"
BUILD = REPO / "build"
STAGE = BUILD / "layer" / "python"  # layers must nest packages under python/
ZIP_PATH = BUILD / "layer.zip"

EXCLUDED = ("boto3", "botocore")  # provided by the Lambda runtime
UNZIPPED_BUDGET_MB = 250  # hard AWS limit for function + layers combined


def _filtered_requirements() -> str:
    lines = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        name = line.split("#")[0].strip().lower()
        if not name or name.startswith(EXCLUDED):
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def main() -> int:
    if STAGE.parent.exists():
        shutil.rmtree(STAGE.parent)
    STAGE.mkdir(parents=True)

    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(_filtered_requirements())
        req_path = tmp.name

    print("installing manylinux wheels for python3.13 / x86_64 ...")
    subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted inputs
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            req_path,
            "--target",
            str(STAGE),
            "--platform",
            "manylinux2014_x86_64",
            "--implementation",
            "cp",
            "--python-version",
            "3.13",
            "--only-binary=:all:",
            "--upgrade",
            "--quiet",
        ],
        check=True,
    )

    for cache_dir in STAGE.rglob("__pycache__"):
        shutil.rmtree(cache_dir)

    print(f"zipping -> {ZIP_PATH} ...")
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(STAGE.parent.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(STAGE.parent).as_posix())

    unzipped_mb = sum(f.stat().st_size for f in STAGE.rglob("*") if f.is_file()) / 1e6
    zipped_mb = ZIP_PATH.stat().st_size / 1e6
    print(f"layer: {zipped_mb:.0f} MB zipped, {unzipped_mb:.0f} MB unzipped")
    if unzipped_mb > UNZIPPED_BUDGET_MB - 20:
        print(f"WARNING: close to the {UNZIPPED_BUDGET_MB} MB unzipped limit")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
