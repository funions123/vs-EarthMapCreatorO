using System;
using Vintagestory.ServerMods;

namespace EarthMapCreator;

/// <summary>Projection of the authoritative EMCL grid into Vintage Story's
/// vegetation-facing packed region climate map.</summary>
public sealed class MapLayerFromClimate : MapLayerBase
{
    private readonly EarthClimate climate;
    private readonly int scale;

    public MapLayerFromClimate(long seed, EarthClimate climate, int scale) : base(seed)
    {
        this.climate = climate;
        this.scale = scale;
    }

    public override int[] GenLayer(int xCoord, int zCoord, int sizeX, int sizeZ)
    {
        int[] result = new int[sizeX * sizeZ];
        for (int z = 0; z < sizeZ; z++)
        {
            int blockZ = (zCoord + z) * scale;
            for (int x = 0; x < sizeX; x++)
            {
                int blockX = (xCoord + x) * scale;
                int temp = Vintagestory.API.Common.Climate.DescaleTemperature(climate.AnnualTemperature(blockX, blockZ));
                int moisture = (int)MathF.Round(climate.VegetationWetness(blockX, blockZ) * 255);
                result[z * sizeX + x] = (temp << 16) | (moisture << 8);
            }
        }
        return result;
    }
}
