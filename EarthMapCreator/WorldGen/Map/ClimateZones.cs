namespace EarthMapCreator;

// 1. A clean enum to identify your new fixed zones
// (This remains unchanged)
public enum ClimateZone
{
    IceCap = 0,
    Tundra = 1,
    HotDesert = 2,
    Steppe = 3,
    Boreal = 4,
    WarmTemperate = 5,
    CoolTemperate = 6,
    Savannah = 7,
    TropicalRainforest = 8,
    Mediterranean = 9,
    ColdDesert = 10,
    SemiDesert = 11
}

public static class ClimateMatcher
{
    /// <summary>
    /// Converts a ClimateZone ID back into representative in-game
    /// temperature and rainfall values.
    /// </summary>
    /// <param name="zone">The ClimateZone enum (or int ID)</param>
    /// <returns>A tuple containing the (temp, rainRel) floats</returns>
    public static (float temp, float rainRel) GetClimateValues(ClimateZone zone)
    {
        switch (zone)
        {
            // Target: -50f to -17f, 0f to 1f
            case ClimateZone.IceCap:
                return (temp: -20f, rainRel: 0.5f);

            // Target: -17f to -10f, 0f to 1f
            case ClimateZone.Tundra:
                return (temp: -13f, rainRel: 0.3f);

            // Target: -50f to 100f, 0f to 0.12f
            case ClimateZone.HotDesert:
                return (temp: 25f, rainRel: 0.1f); // Hot and dry
            
            case ClimateZone.ColdDesert:
                return (temp: 15f, rainRel: 0.1f); // Hot and dry
            
            case ClimateZone.SemiDesert:
                return (temp: 22f, rainRel: 0.2f);

            // Target: -50f to 100f, 0.12f to 0.31f
            case ClimateZone.Steppe:
                return (temp: 17f, rainRel: 0.35f); // Mild and semi-dry

            // Target: -10f to 12f, 0.20f to 1f
            case ClimateZone.Boreal:
                return (temp: 1f, rainRel: 0.6f);

            // Target: 28f to 100f, 0.31f to 0.55f
            case ClimateZone.Savannah:
                return (temp: 27f, rainRel: 0.3f);

            // Target: 26f to 100f, 0.72f to 1f
            case ClimateZone.TropicalRainforest:
                return (temp: 27f, rainRel: 0.9f);
            
            case ClimateZone.Mediterranean:
                return (temp: 19f, rainRel: 0.35f);

            // Fallback: Temperate Forest
            // (Based on C-climate ranges, e.g. 2f-22f, 0.37f-1f)
            case ClimateZone.WarmTemperate:
            default:
                return (temp: 15f, rainRel: 0.7f);
        }
    }
}