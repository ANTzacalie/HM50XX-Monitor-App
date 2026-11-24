"""Package the editable project that corresponds to the Windows release."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


PROJECT_FILES = (
    ".gitignore",
    "LICENSE",
    "README.md",
    "build.ps1",
    "main.py",
    "packaging/SpectralMonitor.iss",
    "packaging/spectral-monitor.ico",
    "requirements-build.txt",
    "requirements.txt",
)
SOURCE_PATTERNS = (
    "hameg_studio/*.py",
    "packaging/*.py",
    "packaging/licenses/*.txt",
    "tests/*.py",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="Output ZIP file")
    args = parser.parse_args()

    project = Path(__file__).resolve().parents[1]
    paths = [project / name for name in PROJECT_FILES]
    for pattern in SOURCE_PATTERNS:
        paths.extend(project.glob(pattern))

    with ZipFile(args.destination, "w") as archive:
        for path in sorted(paths):
            entry = ZipInfo(path.relative_to(project).as_posix())
            entry.create_system = 3  # Use the same regular-file permissions on every build host.
            entry.external_attr = 0o100644 << 16
            entry.compress_type = ZIP_DEFLATED
            archive.writestr(entry, path.read_bytes(), compresslevel=9)


if __name__ == "__main__":
    main()
