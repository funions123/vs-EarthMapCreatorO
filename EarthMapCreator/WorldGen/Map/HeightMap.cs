using System;
using SixLabors.ImageSharp;
using SixLabors.ImageSharp.PixelFormats;
using Vintagestory.API.Datastructures;

namespace EarthMapCreator;

public class HeightMap : DataMap<Rgb48>
{
    private const double ByteToRgb48 = 257.0;

    public HeightMap(string filePath, string landcoverFile) : base(filePath)
    {
        Image<Rgb24> landcoverBmp = LoadBitmap<Rgb24>(landcoverFile);
        
        var watch = System.Diagnostics.Stopwatch.StartNew();
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
                        int posX = x * 512 + i;
                        int posZ = z * 512 + j;
                        Rgb24 lcPixel = landcoverBmp[posX, posZ];
                        Rgb48 heightPixel = Bitmap[posX, posZ];

                        bool isLand = lcPixel.R > 0;
                        int height = 0; // Default to 0 for ocean areas

                        if (isLand)
                        {
                            // heightmap.png stores absolute world Y in its byte value.
                            // ImageSharp expands grayscale bytes to Rgb48 by multiplying by 257.
                            height = Math.Max(1, (int)Math.Round(heightPixel.R / ByteToRgb48));
                        }

                        IntValues[x][z].SetInt(i, j, height);
                    }
                }
            }
        }

        landcoverBmp.Dispose();
        Bitmap.Dispose();
        watch.Stop();
        
        Console.WriteLine("Created heightmap in {0}ms", watch.ElapsedMilliseconds);
    }

}