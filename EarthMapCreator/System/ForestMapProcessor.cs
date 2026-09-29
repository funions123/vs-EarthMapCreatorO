using System;

namespace EarthMapCreator;

internal static class ForestMapProcessor
{
    public static readonly Func<int, int, int, int> ForestPostProcess = ForestDensity;
    public static readonly Func<int, int, int, int> ShrubPostProcess = ShrubDensity;

    private static int ForestDensity(int biome, int blockX, int blockZ) =>
        ProfileAt(biome, blockX, blockZ).Forest;

    private static int ShrubDensity(int biome, int blockX, int blockZ) =>
        ProfileAt(biome, blockX, blockZ).Shrub;

    private static VegetationProfile ProfileAt(int biome, int blockX, int blockZ)
    {
        RegionStore layers = EarthMapCreator.Layers;
        EarthClimate climate = EarthMapCreator.ClimateData;
        if (biome == 0 || layers == null || climate == null ||
            !layers.Contains(blockX, blockZ) || !climate.Contains(blockX, blockZ) ||
            layers.Get(MapPlane.LandMask, blockX, blockZ) == 0 ||
            layers.Get(MapPlane.LakeMask, blockX, blockZ) != 0 ||
            layers.Get(MapPlane.River, blockX, blockZ) != 0)
        {
            return default;
        }

        return PotentialVegetation.Get(
            biome,
            climate.AnnualTemperature(blockX, blockZ),
            climate.WarmestMonthTemperature(blockX, blockZ),
            climate.VegetationWetness(blockX, blockZ));
    }
}