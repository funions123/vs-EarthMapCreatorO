#!/usr/bin/env python3
"""Package the runnable Geo source pipeline without local data or generated files."""

from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parent
GEO = ROOT / "Geo"
VERSION_PATTERN = re.compile(r"<ModVersion>([^<]+)</ModVersion>")


def main() -> None:
    project = (ROOT / "EarthMapCreator" / "EarthMapCreator.csproj").read_text(encoding="utf-8-sig")
    match = VERSION_PATTERN.search(project)
    if match is None:
        raise ValueError("Missing ModVersion in EarthMapCreator.csproj")
    version = match.group(1)
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?", version):
        raise ValueError(f"Invalid release version: {version!r}")

    files = [ROOT / "README", ROOT / "LICENSE", GEO / "requirements.txt",
             GEO / "launch.py", GEO / "config.py", GEO / "benchmark.py"]
    for package in ("pipeline", "util"):
        source = GEO / package
        files.extend(sorted(source.glob("*.py")))
    missing = [str(path.relative_to(ROOT)) for path in files if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing distribution files: {', '.join(missing)}")

    destination = ROOT / "Releases" / f"earthmapcreatoro_geo_{version}.zip"
    destination.parent.mkdir(exist_ok=True)
    temporary = destination.with_suffix(".zip.tmp")
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
            for path in files:
                name = path.relative_to(ROOT).as_posix()
                info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, path.read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Created {destination} ({len(files)} files)")


if __name__ == "__main__":
    main()
