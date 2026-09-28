# config.py — edit this before each run

# --- Bounding Box (EPSG:4326) ---
# Western/Central North America: south, west, north, east = 30.59, -120.00, 49.00, -94.04.
LAT_MIN = 30.59
LAT_MAX = 49.00
LON_MIN = -120.00
LON_MAX = -94.04

# --- Output Resolution ---
# Vintage Story's "25k blocks" world preset is 25,600 (50 regions of 512).
FINAL_WIDTH = 25600
FINAL_LENGTH = 25600
FINAL_RES = 300        # internal raster pixel size in metres

# --- Projection ---
# Set to None to auto-select UTM (for regions < 13° wide) or EPSG:3857 (wider).
# Set to a CRS string (e.g. "EPSG:32633", "ESRI:54080") to override.
FORCE_FINAL_PROJ = None

# --- Resize final PNGs to FINAL_WIDTH x FINAL_LENGTH ---
RESIZE_MAP = True

# --- Terrain Elevation Encoding ---
# Zero metres maps to this absolute game Y. All ocean and land surfaces share it.
# The highest positive elevation on the final map maps to Y=255.
TERRAIN_SEA_LEVEL_Y = 60
# Natural Earth "Alkaline Lake" polygons are saline automatically. Add exact
# "name" field values here for salt lakes labeled only as "Lake" in that data.
# Unlisted "Lake" polygons remain freshwater; reservoirs remain excluded.
SALINE_LAKE_NAMES = []

# --- Bathymetry Scaling ---
ENABLE_BATHY_CUSTOM_SCALE = True

# Shallow ocean and coastline use TERRAIN_SEA_LEVEL_Y.

# Vintage Story maximum ocean depth byte value (0–255); default 50
BATHY_SCALE_MAXDEPTH = 50

# Set True for piecewise scaling (exaggerates shallow water), False for linear
BATHY_USE_PIECEWISE_SCALE = True

# Raw elevation (negative metres) where the piecewise function switches
BATHY_EXAGGERATE_THRESHOLD = -100

# Output byte value at the deep/shallow threshold, between MAXDEPTH and sea level.
BATHY_EXAGGERATE_MIDPOINT = 55

# --- HydroRIVERS ---
# Centerline widths grow with the square root of mean discharge (DIS_AV_CMS).
# At the reference flow, the channel gains one output block over its minimum.
RIVER_MIN_WIDTH_PIXELS = 3.0
RIVER_WIDTH_REFERENCE_FLOW_CMS = 5.4
RIVER_MAX_WIDTH_PIXELS = 8.0
# Suppress reaches whose long-term mean flow is below 1 m³/s; a zero-flow
# reach across Badwater Basin is mapped despite its large drainage area.
# This is a coarse dry-channel filter, not a seasonal-river classification.
RIVER_MIN_FLOW_CMS = 1.0
# Include reaches draining at least this many km² upstream. Set to 0 for all
# HydroRIVERS reaches. Higher values remove smaller headwaters and tributaries.
RIVER_MIN_UPSTREAM_AREA_SQKM = 1000.0
# Scale the area threshold up when output is smaller than the 25,600-block
# reference world. Halving the shortest map dimension doubles the threshold.
RIVER_AUTO_SCALE_UPSTREAM_AREA = True
# Extend large, mapped terminal rivers across short sea-level deltas to the
# nearest coastline. This approximates pre-diversion flow where the source
# centerline now ends inland (e.g. the Colorado River delta).
RIVER_COASTAL_CONNECTION_MIN_UPSTREAM_AREA_SQKM = 10000.0
RIVER_COASTAL_CONNECTION_MAX_DISTANCE_METRES = 30000.0
RIVER_SURFACE_WINDOW_BLOCKS = 5
RIVER_MIN_DEPTH_BLOCKS = 1
RIVER_MAX_DEPTH_BLOCKS = 3
RIVER_BANK_WIDTH_BLOCKS = 3
RIVER_BANK_SLOPE = 1

# --- Dataset URLs ---
OSM_LANDPOLYGONS_URL = "https://osmdata.openstreetmap.de/download/land-polygons-complete-4326.zip"
