using SixLabors.ImageSharp.PixelFormats;
using Vintagestory.API.Datastructures;

namespace EarthMapCreator;

/// <summary>Loads absolute river water-surface Y values from river_surface.png.</summary>
public class RiverSurfaceMap : DataMap<Rgb24>
{
    public RiverSurfaceMap(string filePath) : base(filePath)
    {
        int xRegions = Bitmap.Width / 512;
        int zRegions = Bitmap.Height / 512;
        IntValues = new IntDataMap2D[xRegions][];
        for (int x = 0; x < xRegions; x++)
        {
            IntValues[x] = new IntDataMap2D[zRegions];
            for (int z = 0; z < zRegions; z++)
            {
                IntValues[x][z] = IntDataMap2D.CreateEmpty();
                IntValues[x][z].Size = 512;
                IntValues[x][z].Data = new int[512 * 512];
                for (int i = 0; i < 512; i++)
                {
                    for (int j = 0; j < 512; j++)
                    {
                        Rgb24 pixel = Bitmap[x * 512 + i, z * 512 + j];
                        IntValues[x][z].SetInt(i, j, pixel.R);
                    }
                }
            }
        }
        Bitmap.Dispose();
    }
}
