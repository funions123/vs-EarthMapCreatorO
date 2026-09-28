"""Classify Natural Earth lake records consistently across pipeline stages."""

NATURAL_LAKE_CLASSES = frozenset(("Lake", "Alkaline Lake"))

# Natural Earth 5.0.0 labels these current-map reservoirs as "Lake". OpenStreetMap
# classifies each as water=reservoir. Prefer stable Wikidata IDs; exact names are
# the fallback for source versions or records without an ID.
OSM_RESERVOIR_WIKIDATA_IDS = frozenset((
    "Q451522",   # Lake Havasu
    "Q5034204",  # Canyon Ferry Lake
    "Q1589175",  # Jackson Lake
    "Q6478519",  # Lake Winnibigoshish
    "Q6476029",  # Lake Granby
    "Q3214923",  # Hebgen Lake
    "Q6478379",  # Lake Walcott
))
OSM_RESERVOIR_NAMES = frozenset((
    "Lake Havasu",
    "Canyon Ferry Lake",
    "Jackson Lake",
    "Lake Winnibigoshish",
    "Hebgen Lake",
    "Lake Walcott",
    "Lake Granby",
))


def is_natural_lake(properties) -> bool:
    """Return whether a Natural Earth record is a natural fresh or saline lake."""
    return (
        properties.get("featurecla") in NATURAL_LAKE_CLASSES
        and properties.get("wikidataid") not in OSM_RESERVOIR_WIKIDATA_IDS
        and properties.get("name") not in OSM_RESERVOIR_NAMES
    )
