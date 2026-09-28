using System;

namespace EarthMapCreator;

internal static class ForestMapProcessor
{
    public static System.Func<int, int, int, int> ForestPostProcess = (val, blockX, blockZ) =>
    {
        // Failsafe check for coordinates outside the map bounds.
        // This check will now correctly catch negative regions.
        if (!EarthMapCreator.Layers.Contains(blockX, blockZ))
        {
            return 0; // Outside the map, so no trees.
        }
        
        // Get the landmask value for the current pixel.
        int landmaskValue = EarthMapCreator.Layers.Get(MapPlane.LandMask, blockX, blockZ);

        // If the landmask value is 0 (or whatever signifies water), return 0 for tree density.
        if (landmaskValue == 0)
        {
            return 0;
        }

        // The pixel is on land, so proceed with the original tree density calculation.
        byte trees = (byte)val;
        
        trees = (byte)(EarthMapCreator.config.ForestMulti * val);
        return trees;
    };
}