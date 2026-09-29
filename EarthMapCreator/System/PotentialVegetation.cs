using System;

namespace EarthMapCreator;

public readonly record struct VegetationProfile(
    byte Forest,
    byte Shrub,
    float Grass,
    float Fertility,
    int SoilDepth,
    bool Bare,
    bool Snow);

/// <summary>Converts a Hengl 2018 PNV biome class and local CHELSA climate into worldgen cover.</summary>
public static class PotentialVegetation
{
    private static readonly VegetationProfile None = new(0, 0, 0f, 0f, 0, false, false);
    public const float TreeGrowingSeasonTemperature = 10f;

    public static VegetationProfile Get(int biome, float annualTemperature, float warmestTemperature, float wetness)
    {
        if (biome == 0 || !float.IsFinite(annualTemperature) ||
            !float.IsFinite(warmestTemperature) || !float.IsFinite(wetness))
        {
            return None;
        }

        VegetationProfile potential = biome switch
        {
            1 => new(245, 35, 0.15f, 0.65f, 4, false, false), // tropical evergreen broadleaf forest
            2 => new(225, 55, 0.30f, 0.65f, 4, false, false), // tropical semi-evergreen broadleaf forest
            3 => new(175, 90, 0.55f, 0.65f, 3, false, false), // tropical deciduous forest and woodland
            4 => new(225, 45, 0.30f, 0.65f, 4, false, false), // warm-temperate evergreen and mixed forest
            7 => new(245, 55, 0.35f, 0.65f, 4, false, false), // cool-temperate rainforest
            8 => new(225, 35, 0.30f, 0.35f, 3, false, false), // cool evergreen needleleaf forest
            9 => new(230, 50, 0.35f, 0.65f, 4, false, false), // cool mixed forest
            13 => new(220, 50, 0.40f, 0.65f, 4, false, false), // temperate deciduous broadleaf forest
            14 => new(180, 65, 0.45f, 0.35f, 3, false, false), // cold deciduous forest
            15 => new(205, 45, 0.30f, 0.35f, 3, false, false), // cold evergreen needleleaf forest
            16 => new(95, 155, 0.65f, 0.35f, 2, false, false), // temperate sclerophyll woodland and shrubland
            17 => new(120, 110, 0.60f, 0.35f, 2, false, false), // temperate evergreen needleleaf open woodland
            18 => new(70, 110, 0.90f, 0.35f, 2, false, false), // tropical savanna
            19 => new(80, 110, 0.85f, 0.35f, 2, false, false), // temperate deciduous broadleaf savanna
            20 => new(35, 155, 0.45f, 0.10f, 1, true, false), // xerophytic woods/scrub
            22 => new(5, 70, 0.95f, 0.35f, 2, false, false), // steppe
            27 => new(0, 25, 0.10f, 0.10f, 1, true, false), // desert: mostly mineral, occasional thin soil
            28 => new(0, 40, 0.75f, 0.10f, 1, false, false), // graminoid and forb tundra
            30 => new(0, 130, 0.45f, 0.10f, 1, false, false), // erect dwarf shrub tundra
            31 => new(0, 170, 0.40f, 0.10f, 1, false, false), // low and high shrub tundra
            32 => new(0, 80, 0.35f, 0.10f, 1, false, false), // prostrate dwarf shrub tundra
            _ => None
        };
        if (potential == None) return None;

        wetness = Math.Clamp(wetness, 0f, 1f);
        float snowThreshold = EarthMapCreator.config?.SnowpackWarmestMonthTemperature ?? 5f;
        bool snow = warmestTemperature < snowThreshold;

        float warmGrowth = SmoothStep(snowThreshold, EarthClimate.GrassGrowingSeasonTemperature, warmestTemperature);
        float annualGrowth = SmoothStep(-18f, -2f, annualTemperature);
        float heatTolerance = 1f - SmoothStep(32f, 42f, annualTemperature);
        float temperatureGrowth = Math.Min(warmGrowth, Math.Min(annualGrowth, heatTolerance));
        float treeMoisture = SmoothStep(0.07f, 0.55f, wetness);
        float undergrowthMoisture = SmoothStep(0.015f, 0.25f, wetness);

        byte forest = warmestTemperature < TreeGrowingSeasonTemperature ? (byte)0 :
            ScaleByte(potential.Forest, temperatureGrowth * treeMoisture);
        byte shrub = ScaleByte(potential.Shrub, temperatureGrowth * (0.35f + 0.65f * undergrowthMoisture));
        float grass = potential.Grass * temperatureGrowth * undergrowthMoisture;

        float fertility = potential.Fertility;
        if (snow || wetness < 0.08f) fertility = 0.10f;
        else if (fertility > 0.35f && wetness < 0.20f) fertility = 0.35f;

        int soilDepth = potential.SoilDepth;
        if (snow || wetness < 0.04f) soilDepth = Math.Min(soilDepth, 1);

        return new VegetationProfile(forest, shrub, grass, fertility, soilDepth, potential.Bare, snow);
    }

    private static byte ScaleByte(byte value, float factor) =>
        (byte)Math.Clamp((int)MathF.Round(value * Math.Clamp(factor, 0f, 1f)), 0, 255);

    private static float SmoothStep(float low, float high, float value)
    {
        float t = Math.Clamp((value - low) / (high - low), 0f, 1f);
        return t * t * (3f - 2f * t);
    }
}
