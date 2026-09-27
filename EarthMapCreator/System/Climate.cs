using System;
using Vintagestory.API.Common;
using Vintagestory.API.Datastructures;
using Vintagestory.API.Server;

namespace EarthMapCreator;

public class Climate : ModSystem
{

    public static System.Func<int, int, int, int> ClimatePostProcess = (val, blockX, blockZ) =>
    {
        // Failsafe check for coordinates outside the map bounds.
        // This check will now correctly catch negative regions.
        if (!EarthMapCreator.Layers.Contains(blockX, blockZ))
        {
            // Outside map, return a default climate (e.g., Temperate)
            (float temp, float rainRel) fallback = ClimateMatcher.GetClimateValues(ClimateZone.Mediterranean);
            val = PackClimate(fallback.temp, fallback.rainRel);
            return val;
        }
        
        // 1. Get the pre-processed Zone ID from your map
        int zoneId = val;
        ClimateZone zone = (ClimateZone)zoneId;

        // 2. Convert that Zone ID back into representative Temp/Rain values
        (float temp, float rainRel) climate = ClimateMatcher.GetClimateValues(zone);

        if (EarthMapCreator.Layers.Get(MapPlane.River, blockX, blockZ) != 0)
        {
            climate.rainRel = 0.9f;
        }

        // 3. Convert Temp/Rain floats back into 0-255 byte values
        // Map Temp (-30 to 40) back to Red (0-255)
        float red_f = ((climate.temp + 30f) / 70f) * 255f;
        // Map Rain (0.0 to 1.0) back to Green (0-255)
        float green_f = climate.rainRel * 255f;

        byte red = (byte)Math.Clamp(red_f, 0, 255);
        byte green = (byte)Math.Clamp(green_f, 0, 255);

        // 4. Apply the config modifications *exactly* as the old code did.
        // This preserves your config's behavior.
        red += EarthMapCreator.config.TemperatureAdd;
        green += EarthMapCreator.config.PrecipitationAdd;

        red = (byte)(EarthMapCreator.config.TemperatureMulti * red);
        green = (byte)(EarthMapCreator.config.PrecipitationMulti * green); // Note: Typo in original file was PrecipitationMulti

        // 5. Pack the final bytes into an integer
        int rgb = red;
        rgb = (rgb << 8) + green;
        rgb = (rgb << 8) + 0; // Blue channel is 0

        return rgb;
    };
    
    // This helper function was added for readability
    private static int PackClimate(float temp, float rainRel)
    {
        float red_f = ((temp + 30f) / 70f) * 255f;
        float green_f = rainRel * 255f;
        byte red = (byte)Math.Clamp(red_f, 0, 255);
        byte green = (byte)Math.Clamp(green_f, 0, 255);
        
        int rgb = red;
        rgb = (rgb << 8) + green;
        rgb = (rgb << 8) + 0;
        return rgb;
    }
    
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