"""
pipeline/land.py — land mask, rivers, lakes.
Replaces land.sh (OSM land polygons + Natural Earth rivers + Natural Earth lakes).
"""
import zipfile
from pathlib import Path

import numpy as np
import fiona
import fiona.crs
from shapely.geometry import box, shape, mapping
from shapely.ops import transform as shp_transform
from pyproj import Transformer

from util.projection import MasterGrid, Bounds4326
from util.raster import rasterize_features, save_array, download_file
from pipeline.lake_classification import is_natural_lake


def _get_dataset(datasets_dir: Path, url: str) -> Path:
    """Use the cached OSM archive, downloading it once if absent."""
    archive = datasets_dir / "land-polygons-complete-4326.zip"
    if archive.exists():
        return archive
    datasets_dir.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix(".zip.tmp")
    try:
        download_file(url, str(temporary), desc=archive.name)
        with zipfile.ZipFile(temporary) as source:
            if not any(Path(name).name == "land_polygons.shp" for name in source.namelist()):
                raise ValueError(f"Missing land_polygons.shp in {archive.name}")
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    return archive


def _find_file(directory: Path, pattern: str):
    """Glob for a file pattern in directory, return first match or None."""
    matches = list(directory.rglob(pattern))
    return matches[0] if matches else None


def _reproject_geoms(geoms, src_crs_str: str, dst_crs) -> list:
    """Reproject a list of shapely geometries from src_crs to dst_crs."""
    transformer = Transformer.from_crs(src_crs_str, dst_crs, always_xy=True)
    return [shp_transform(transformer.transform, g) for g in geoms]


def run(work_dir: Path, datasets_dir: Path, grid: MasterGrid, bounds: Bounds4326, cfg):
    """
    Produces:
      - work_dir/land_osm_mask.tif   (Byte, 255=land, 0=ocean)
      - work_dir/crop_lakes.gpkg     (vector lake polygons clipped to bbox)
    """
    osm_dir = work_dir / "osm_processing"
    osm_dir.mkdir(parents=True, exist_ok=True)

    lon_min, lat_min, lon_max, lat_max = bounds
    bbox_geom = box(lon_min, lat_min, lon_max, lat_max)

    # ------------------------------------------------------------------ #
    # 1. Land mask
    # ------------------------------------------------------------------ #
    land_zip = _get_dataset(datasets_dir, cfg.OSM_LANDPOLYGONS_URL)

    land_extract = osm_dir / "land_polygons_extracted"
    if not (land_extract / "land_polygons.shp").exists():
        land_extract.mkdir(exist_ok=True)
        with zipfile.ZipFile(land_zip) as z:
            z.extractall(land_extract)
        # The zip has a subdir; move files up if needed
        subdirs = [d for d in land_extract.iterdir() if d.is_dir()]
        if subdirs:
            sub = subdirs[0]
            for f in sub.iterdir():
                f.rename(land_extract / f.name)
            sub.rmdir()

    land_shp = land_extract / "land_polygons.shp"
    if not land_shp.exists():
        # Try recursive search
        found = _find_file(land_extract, "land_polygons.shp")
        if found:
            land_shp = found
        else:
            raise FileNotFoundError(f"land_polygons.shp not found in {land_extract}")

    land_shapes = _clip_and_project_shapes(
        str(land_shp), bbox_geom, lon_min, lat_min, lon_max, lat_max,
        grid, burn_value=255, src_crs="EPSG:4326",
    )
    rasterize_features(
        land_shapes,
        grid,
        str(work_dir / "land_osm_mask.tif"),
        burn_value=255,
        dtype="uint8",
        all_touched=False,
        fill=0,
    )
    print("[land] Land mask done.")


    # 2. Lakes
    # ------------------------------------------------------------------ #
    lakes_zip_path = datasets_dir / "ne_10m_lakes.zip"
    if not lakes_zip_path.exists():
        raise FileNotFoundError(
            f"Lakes dataset not found at {lakes_zip_path}. "
            "Please place ne_10m_lakes.zip in the datasets/ directory."
        )

    lakes_extract = osm_dir / "lakes_extracted"
    if not lakes_extract.exists() or not any(lakes_extract.iterdir()):
        lakes_extract.mkdir(exist_ok=True)
        with zipfile.ZipFile(lakes_zip_path) as z:
            z.extractall(lakes_extract)

    lakes_shp = _find_file(lakes_extract, "ne_10m_lakes.shp")
    if not lakes_shp:
        raise FileNotFoundError(f"ne_10m_lakes.shp not found in {lakes_extract}")

    _clip_lakes_to_gpkg(
        str(lakes_shp),
        str(work_dir / "crop_lakes.gpkg"),
        lon_min, lat_min, lon_max, lat_max,
        bbox_geom,
    )
    print("[land] Lakes done.")


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #



def _clip_and_project_shapes(
    src_path: str,
    bbox_geom,
    lon_min, lat_min, lon_max, lat_max,
    grid: MasterGrid,
    burn_value,
    src_crs: str = "EPSG:4326",
):
    """
    Read features from src_path within bbox, clip, reproject to grid.crs,
    return list of (geometry_dict, burn_value) tuples for rasterize.
    """
    transformer = Transformer.from_crs(src_crs, grid.crs, always_xy=True)
    shapes = []
    with fiona.open(src_path, bbox=(lon_min, lat_min, lon_max, lat_max)) as src:
        for feat in src:
            geom = shape(feat["geometry"])
            clipped = geom.intersection(bbox_geom)
            if clipped.is_empty:
                continue
            proj_geom = shp_transform(transformer.transform, clipped)
            shapes.append((proj_geom.__geo_interface__, burn_value))
    return shapes




def _clip_lakes_to_gpkg(
    src_path: str,
    dst_path: str,
    lon_min, lat_min, lon_max, lat_max,
    bbox_geom,
):
    """Clip natural lake polygons to a GeoPackage, excluding known reservoirs."""
    if Path(dst_path).exists():
        Path(dst_path).unlink()

    with fiona.open(src_path, bbox=(lon_min, lat_min, lon_max, lat_max)) as src:
        if "featurecla" not in src.schema["properties"]:
            raise ValueError("Natural Earth lake dataset is missing featurecla")
        meta = src.meta.copy()
        meta.update(driver="GPKG")
        # Ensure geometry type is compatible (clipping may produce multi-geoms)
        meta["schema"] = {
            "geometry": src.schema["geometry"],
            "properties": src.schema["properties"],
        }

        with fiona.open(dst_path, "w", **meta) as dst:
            for feat in src:
                if not is_natural_lake(feat["properties"]):
                    continue
                geom = shape(feat["geometry"])
                clipped = geom.intersection(bbox_geom)
                if not clipped.is_empty:
                    dst.write({
                        "type": "Feature",
                        "geometry": mapping(clipped),
                        "properties": dict(feat["properties"]),
                    })
