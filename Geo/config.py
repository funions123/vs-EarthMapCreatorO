# config.py — edit this before each run

# --- Bounding Box (EPSG:4326) ---
# Southern Levant: square 3 by 3 degree test extent in UTM 36N.
# Covers the requested 30-33 N, 34-35 E area and includes the full Dead Sea.
LAT_MIN = 30.0
LAT_MAX = 33.0
LON_MIN = 33.0
LON_MAX = 36.0

# --- Output Resolution ---
FINAL_WIDTH = 10240    # final PNG width in pixels (must be multiple of 512)
FINAL_LENGTH = 10240   # final PNG height in pixels (must be multiple of 512)
FINAL_RES = 300        # internal raster pixel size in metres

# --- Projection ---
# Set to None to auto-select UTM (for regions < 13° wide) or EPSG:3857 (wider).
# Set to a CRS string (e.g. "EPSG:32633", "ESRI:54080") to override.
FORCE_FINAL_PROJ = "EPSG:32636"

# --- Resize final PNGs to FINAL_WIDTH x FINAL_LENGTH ---
RESIZE_MAP = True

# --- Terrain Elevation Encoding ---
# World Y assigned to zero metres elevation and metres represented per block.
# Negative elevations remain below TERRAIN_SEA_LEVEL_Y instead of being clipped.
TERRAIN_SEA_LEVEL_Y = 92
TERRAIN_METRES_PER_BLOCK = 10.0
TERRAIN_MAX_Y = 250

# --- Bathymetry Scaling ---
ENABLE_BATHY_CUSTOM_SCALE = True

# Vintage Story sea level byte value (0–255); default 92
BATHY_SCALE_SEALEVEL = 92

# Vintage Story maximum ocean depth byte value (0–255); default 50
BATHY_SCALE_MAXDEPTH = 50

# Set True for piecewise scaling (exaggerates shallow water), False for linear
BATHY_USE_PIECEWISE_SCALE = True

# Raw elevation (negative metres) where the piecewise function switches
BATHY_EXAGGERATE_THRESHOLD = -100

# Output byte value at the threshold (must be between MAXDEPTH and SEALEVEL)
BATHY_EXAGGERATE_MIDPOINT = 80

# --- OSM Rivers ---
# Line-only waterways are buffered in projected metres; mapped polygons retain
# their actual banks. At ~30 m/block, 45 m generally renders as 2-3 blocks.
RIVER_DEFAULT_WIDTH_METRES = 45.0
RIVER_MAX_LINE_WIDTH_METRES = 250.0
RIVER_SURFACE_WINDOW_BLOCKS = 5
RIVER_MIN_DEPTH_BLOCKS = 1
RIVER_MAX_DEPTH_BLOCKS = 3
RIVER_BANK_WIDTH_BLOCKS = 3
RIVER_BANK_SLOPE = 1

# --- Dataset caching ---
# Cache downloaded datasets in Geo/datasets/ for reuse across runs
DOWNLOAD_DATASETS_LOCALLY = True
FORCE_LOCAL_DATASETS_UPDATE = False
GET_DATASETS_LOCALLY = True

# --- Dataset URLs ---
OSM_LANDPOLYGONS_URL = "https://osmdata.openstreetmap.de/download/land-polygons-complete-4326.zip"
KOPPEN_URL = "https://figshare.com/ndownloader/files/45057352"
