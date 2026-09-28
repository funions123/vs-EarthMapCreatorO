using System;
using System.Buffers.Binary;
using System.IO;
using Vintagestory.API.MathTools;

namespace EarthMapCreator;

/// <summary>
/// EMCL v2: "EMCL", version, map width, map height, spacing (int32 LE), lapse
/// (float32 °C per source block), then Z-major samples of 12 signed
/// temperatures (tenths °C), annual P and PET (unsigned millimetres), and the
/// mean terrain Y of the sample's cell (unsigned tenths of a source block).
/// Sample (x,z) is at block (x*spacing,z*spacing).
/// </summary>
public sealed class EarthClimate
{
    private const int HeaderSize = 24;
    private const int SampleSize = 30;
    public const float GrassGrowingSeasonTemperature = 12f;
    private readonly byte[] samples;
    private readonly float[] meanTemperature;
    private readonly float[] warmestTemperature;
    private readonly float[] wetness;
    private readonly float[] precipitation;
    private readonly float[] potentialEvapotranspiration;
    private readonly float[] referenceY;
    private readonly float lapsePerBlock;
    private readonly int width;
    private readonly int height;
    private readonly int spacing;
    private readonly int mapWidth;
    private readonly int mapHeight;

    public int MapWidth => mapWidth;
    public int MapHeight => mapHeight;

    public EarthClimate(string path, int mapWidth, int mapHeight) : this(File.ReadAllBytes(path), mapWidth, mapHeight) { }

    public EarthClimate(byte[] data, int mapWidth, int mapHeight)
    {
        if (data.Length < HeaderSize || data[0] != 'E' || data[1] != 'M' || data[2] != 'C' || data[3] != 'L'
            || BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(4)) != 2)
            throw new InvalidDataException("Unsupported Earth climate file; rebuild earthclimate.bin (EMCL v2)");

        this.mapWidth = mapWidth;
        this.mapHeight = mapHeight;
        spacing = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(16));
        lapsePerBlock = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(20));
        if (BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(8)) != mapWidth
            || BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(12)) != mapHeight
            || spacing <= 0 || mapWidth <= 0 || mapHeight <= 0 || !float.IsFinite(lapsePerBlock) || lapsePerBlock > 0)
            throw new InvalidDataException("Earth climate dimensions or spacing do not match the region store.");

        width = (mapWidth - 1) / spacing + 1;
        height = (mapHeight - 1) / spacing + 1;
        if ((long)data.Length != HeaderSize + (long)width * height * SampleSize)
            throw new InvalidDataException("Earth climate sample count does not match its header.");

        samples = data;
        meanTemperature = new float[width * height];
        warmestTemperature = new float[width * height];
        wetness = new float[width * height];
        precipitation = new float[width * height];
        potentialEvapotranspiration = new float[width * height];
        referenceY = new float[width * height];
        for (int i = 0; i < meanTemperature.Length; i++)
        {
            int offset = HeaderSize + i * SampleSize;
            float sum = 0;
            float warmest = float.MinValue;
            for (int month = 0; month < 12; month++)
            {
                float temperature = BinaryPrimitives.ReadInt16LittleEndian(data.AsSpan(offset + month * 2)) * 0.1f;
                sum += temperature;
                warmest = Math.Max(warmest, temperature);
            }
            meanTemperature[i] = sum / 12;
            warmestTemperature[i] = warmest;
            float p = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(offset + 24));
            precipitation[i] = p;
            float pet = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(offset + 26));
            potentialEvapotranspiration[i] = pet;
            // P/PET vegetation proxy, capped for very low-P cold deserts.
            wetness[i] = Math.Min(p / (p + Math.Max(pet, 1f)), Math.Min(1f, p / 200f));
            referenceY[i] = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(offset + 28)) * 0.1f;
        }
    }

    public byte[] GetTransferBytes() => samples;

    private float Interpolate(float[] field, int x, int z)
    {
        float fx = Math.Clamp((float)x / spacing, 0, width - 1);
        float fz = Math.Clamp((float)z / spacing, 0, height - 1);
        int x0 = (int)fx, z0 = (int)fz;
        int x1 = Math.Min(x0 + 1, width - 1), z1 = Math.Min(z0 + 1, height - 1);
        float tx = fx - x0, tz = fz - z0;
        float top = field[z0 * width + x0] * (1 - tx) + field[z0 * width + x1] * tx;
        float bottom = field[z1 * width + x0] * (1 - tx) + field[z1 * width + x1] * tx;
        return top * (1 - tz) + bottom * tz;
    }

    public bool Contains(int x, int z) => (uint)x < (uint)mapWidth && (uint)z < (uint)mapHeight;
    public float AnnualTemperature(int x, int z) => Interpolate(meanTemperature, x, z);
    public float VegetationWetness(int x, int z) => Interpolate(wetness, x, z);
    public float WarmestMonthTemperature(int x, int z) => Interpolate(warmestTemperature, x, z);
    public float AnnualPrecipitation(int x, int z) => Interpolate(precipitation, x, z);
    public float AnnualPotentialEvapotranspiration(int x, int z) => Interpolate(potentialEvapotranspiration, x, z);

    /// <summary>
    /// Temperature change at world Y relative to this position's mapped terrain.
    /// CHELSA values describe air at the smoothed terrain surface; air above
    /// that is cooler and below it warmer. Heights are in source (0-255) Y.
    /// </summary>
    public float AltitudeOffset(int x, int z, float sourceY) =>
        lapsePerBlock * (sourceY - Interpolate(referenceY, x, z));

    public float MonthlyTemperature(int x, int z, double yearFraction)
    {
        double monthPosition = ((yearFraction * 12 - 0.5) % 12 + 12) % 12;
        int month = (int)monthPosition;
        int next = (month + 1) % 12;
        float fraction = (float)(monthPosition - month);
        float fx = Math.Clamp((float)x / spacing, 0, width - 1);
        float fz = Math.Clamp((float)z / spacing, 0, height - 1);
        int x0 = (int)fx, z0 = (int)fz;
        int x1 = Math.Min(x0 + 1, width - 1), z1 = Math.Min(z0 + 1, height - 1);
        float tx = fx - x0, tz = fz - z0;
        float top = GameMath.Lerp(MonthAt(x0, z0, month, next, fraction), MonthAt(x1, z0, month, next, fraction), tx);
        float bottom = GameMath.Lerp(MonthAt(x0, z1, month, next, fraction), MonthAt(x1, z1, month, next, fraction), tx);
        return GameMath.Lerp(top, bottom, tz);
    }

    private float MonthAt(int x, int z, int month, int next, float fraction)
    {
        int offset = HeaderSize + (z * width + x) * SampleSize;
        float a = BinaryPrimitives.ReadInt16LittleEndian(samples.AsSpan(offset + month * 2));
        float b = BinaryPrimitives.ReadInt16LittleEndian(samples.AsSpan(offset + next * 2));
        return (a + (b - a) * fraction) * 0.1f;
    }
}
