"""Benchmark cached real-data pipeline stages without overwriting Geo/work.

Run: python benchmark.py --work-dir work/benchmark-before --report work/before.json
Repeat with a different work directory after changes. Reports include decoded
raster and binary hashes, stage wall times, sampled RSS, and process peak RSS.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace

import psutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--size", type=int, default=25600)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    work = args.work_dir.resolve()
    if work.exists():
        parser.error("work directory must not already exist")
    work.mkdir(parents=True)
    # Reuse extracted immutable land files; exclude first-run extraction cost.
    cached = root / "work/osm_processing/land_polygons_extracted"
    if cached.is_dir():
        shutil.copytree(cached, work / "osm_processing/land_polygons_extracted",
                        copy_function=os.link)

    import config
    from launch import _tune_performance
    _tune_performance()
    from util.projection import get_projection, get_master_grid
    from pipeline import land, topography, vegetation, translate
    from pipeline.region_store import bake
    from pipeline.earth_climate import run as climate
    import rasterio

    cfg = SimpleNamespace(**{k: v for k, v in vars(config).items() if k.isupper()})
    cfg.FINAL_WIDTH = cfg.FINAL_LENGTH = args.size
    grid, bounds = get_master_grid(cfg, get_projection(cfg))
    datasets = root / "datasets"
    build = work / "build"
    process = psutil.Process()
    report = {"size": args.size, "master_grid": [grid.width, grid.height],
              "cache": "cached datasets and extracted land; OS cache uncontrolled",
              "stages": {}, "hashes": {}}
    stages = [
        ("land", lambda: land.run(work, datasets, grid, bounds, cfg)),
        ("topography", lambda: topography.run(work, datasets, grid, bounds, cfg)),
        ("vegetation", lambda: vegetation.run(work, datasets, grid, bounds, cfg)),
        ("translate", lambda: translate.run(work, grid, bounds, cfg)),
        ("regions", lambda: bake(build, args.size, args.size,
                                 cfg.TERRAIN_SEA_LEVEL_Y, cfg.BATHY_SCALE_MAXDEPTH)),
        ("climate", lambda: climate(build, datasets, grid, cfg)),
    ]
    started = time.perf_counter()
    for name, run in stages:
        stop = threading.Event()
        peak = [process.memory_info().rss]
        def sample():
            while not stop.wait(0.02):
                peak[0] = max(peak[0], process.memory_info().rss)
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        start = time.perf_counter()
        try:
            run()
        finally:
            stop.set()
            sampler.join()
        report["stages"][name] = {"seconds": time.perf_counter() - start,
                                  "sampled_peak_rss_MiB": peak[0] / 2**20}
        print(name, report["stages"][name], flush=True)
    report["total_seconds"] = time.perf_counter() - started
    memory = process.memory_info()
    report["process_peak_rss_MiB"] = getattr(memory, "peak_wset", max(
        int(s["sampled_peak_rss_MiB"] * 2**20) for s in report["stages"].values())) / 2**20
    # Decoded pixels avoid treating compression differences as map differences.
    for path in sorted(build.iterdir()):
        if path.suffix not in (".png", ".bin", ".regions"):
            continue
        digest = hashlib.sha256()
        if path.suffix == ".png":
            with rasterio.open(path) as source:
                for y in range(0, source.height, 256):
                    window = rasterio.windows.Window(0, y, source.width,
                                                     min(256, source.height - y))
                    digest.update(source.read(window=window).tobytes())
        else:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
        report["hashes"][path.name] = digest.hexdigest()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
