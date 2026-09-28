#!/usr/bin/env python3
"""
launch.py — Python pipeline entry point.
Run from the Geo/ directory:
    python launch.py

Replaces launch.sh + all stage shell scripts.
Requires: pip install -r requirements.txt
No GDAL CLI tools needed.
"""

import os
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
MAIN_DIR = Path(__file__).parent.resolve()
WORK_DIR = MAIN_DIR / "work"
DATASETS_DIR = MAIN_DIR / "datasets"
BUILD_DIR = WORK_DIR / "build"

WORK_DIR.mkdir(exist_ok=True)
DATASETS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Import config
# ---------------------------------------------------------------------------
sys.path.insert(0, str(MAIN_DIR))
import config as cfg

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def log(msg: str):
    ts = time.strftime("%Y-%m-%d_%H:%M:%S")
    print(f"LOG [{ts}] {msg}")


# ---------------------------------------------------------------------------
# Performance tuning: hint rasterio/GDAL about available RAM
# ---------------------------------------------------------------------------
def _tune_performance():
    try:
        import psutil
        ram_mb = psutil.virtual_memory().total // (1024 * 1024)
        gdal_cache = min(ram_mb // 4, 4096)
        os.environ.setdefault("GDAL_CACHEMAX", str(gdal_cache))
        os.environ.setdefault("GDAL_NUM_THREADS", "ALL_CPUS")
    except ImportError:
        os.environ.setdefault("GDAL_CACHEMAX", "512")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    _tune_performance()

    # --- Projection and master grid ---
    from util.projection import get_projection, get_master_grid

    proj_crs = get_projection(cfg)
    grid, bounds = get_master_grid(cfg, proj_crs)

    # --- Pipeline stages ---
    from pipeline import land, topography, tree, translate

    log("=== Stage: land ===")
    land.run(WORK_DIR, DATASETS_DIR, grid, bounds, cfg)

    log("=== Stage: topography ===")
    topography.run(WORK_DIR, DATASETS_DIR, grid, bounds, cfg)

    log("=== Stage: tree ===")
    tree.run(WORK_DIR, DATASETS_DIR, grid, bounds, cfg)

    log("=== Stage: translate ===")
    translate.run(WORK_DIR, grid, bounds, cfg)
    from pipeline.region_store import bake
    from pipeline.earth_climate import run as write_earth_climate
    log("=== Stage: region store ===")
    bake(BUILD_DIR, cfg.FINAL_WIDTH, cfg.FINAL_LENGTH,
         cfg.TERRAIN_SEA_LEVEL_Y, cfg.BATHY_SCALE_MAXDEPTH)
    log("=== Stage: Earth climate ===")
    write_earth_climate(BUILD_DIR, DATASETS_DIR, grid, cfg)

    log("=== Pipeline complete. PNGs, earthmap.regions, earthclimate.bin in: " + str(BUILD_DIR) + " ===")


if __name__ == "__main__":
    main()
