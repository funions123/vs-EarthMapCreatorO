using System;
using System.Buffers.Binary;
using System.Collections.Generic;
using System.IO;
using Microsoft.Win32.SafeHandles;
using Vintagestory.API.MathTools;

namespace EarthMapCreator;

/// <summary>
/// EMCL v2: "EMCL", version, map width, map height, spacing (int32 LE), lapse
/// (float32 °C per source block), then Z-major samples of 12 signed
/// temperatures (tenths °C), annual P and PET (unsigned millimetres), and the
/// mean terrain Y of the sample's cell (unsigned tenths of a source block).
/// Sample (x,z) is at block (x*spacing,z*spacing).
/// </summary>
public sealed class EarthClimate : IDisposable
{
    private const int HeaderSize = 24;
    private const int SampleSize = 30;
    private const int RequiredSpacing = 8;
    private const int ServerCachedTiles = 64;
    internal const int TileWorldSize = 512;
    internal const int TileIntervals = TileWorldSize / RequiredSpacing;
    internal const int TileSamples = TileIntervals + 1;
    internal const int TileByteLength = TileSamples * TileSamples * SampleSize;
    internal const int MaxClientCachedTiles = 256;
    public const float GrassGrowingSeasonTemperature = 12f;

    private readonly object cacheLock = new();
    private readonly Dictionary<long, Tile> tiles = new();
    private readonly LinkedList<Tile> leastRecentlyUsed = new();
    private readonly SafeFileHandle fileHandle;
    private readonly byte[] completeSamples;
    private readonly bool tiledClient;
    private readonly int maxCachedTiles;
    private readonly float lapsePerBlock;
    private readonly int width;
    private readonly int height;
    private readonly int spacing;
    private readonly int mapWidth;
    private readonly int mapHeight;
    private bool disposed;

    public int MapWidth => mapWidth;
    public int MapHeight => mapHeight;
    internal int Spacing => spacing;
    internal float LapsePerBlock => lapsePerBlock;
    internal int CachedTileCount { get { lock (cacheLock) return tiles.Count; } }

    /// <summary>Opens an EMCL file for bounded random access. Sample data is read
    /// in 512-block tiles and retained in a small LRU cache.</summary>
    public EarthClimate(string path, int mapWidth, int mapHeight)
    {
        SafeFileHandle handle = File.OpenHandle(path, FileMode.Open, FileAccess.Read, FileShare.Read,
            FileOptions.RandomAccess);
        try
        {
            Span<byte> header = stackalloc byte[HeaderSize];
            ReadExactly(handle, header, 0);
            ValidateHeader(header, mapWidth, mapHeight, out spacing, out lapsePerBlock, out width, out height);
            long expectedLength = HeaderSize + (long)width * height * SampleSize;
            if (RandomAccess.GetLength(handle) != expectedLength)
                throw new InvalidDataException("Earth climate sample count does not match its header.");

            this.mapWidth = mapWidth;
            this.mapHeight = mapHeight;
            fileHandle = handle;
            completeSamples = null;
            tiledClient = false;
            maxCachedTiles = ServerCachedTiles;
        }
        catch
        {
            handle.Dispose();
            throw;
        }
    }

    /// <summary>Reads an in-memory complete EMCL v2 image. Retained for callers
    /// that already own a complete file; network synchronization uses tile data.</summary>
    public EarthClimate(byte[] data, int mapWidth, int mapHeight)
    {
        if (data == null || data.Length < HeaderSize)
            throw new InvalidDataException("Unsupported Earth climate file; rebuild earthclimate.bin (EMCL v2, spacing 8).");
        ValidateHeader(data.AsSpan(0, HeaderSize), mapWidth, mapHeight,
            out spacing, out lapsePerBlock, out width, out height);
        if ((long)data.Length != HeaderSize + (long)width * height * SampleSize)
            throw new InvalidDataException("Earth climate sample count does not match its header.");

        this.mapWidth = mapWidth;
        this.mapHeight = mapHeight;
        completeSamples = data;
        fileHandle = null;
        tiledClient = false;
        maxCachedTiles = 0;
    }

    private EarthClimate(int mapWidth, int mapHeight, int spacing, float lapsePerBlock)
    {
        if (mapWidth <= 0 || mapHeight <= 0 || spacing != RequiredSpacing
            || !float.IsFinite(lapsePerBlock) || lapsePerBlock > 0)
            throw new InvalidDataException("Invalid Earth climate tile metadata.");

        this.mapWidth = mapWidth;
        this.mapHeight = mapHeight;
        this.spacing = spacing;
        this.lapsePerBlock = lapsePerBlock;
        width = (mapWidth - 1) / spacing + 1;
        height = (mapHeight - 1) / spacing + 1;
        completeSamples = null;
        fileHandle = null;
        tiledClient = true;
        maxCachedTiles = MaxClientCachedTiles;
    }

    internal static EarthClimate CreateTiledClient(int mapWidth, int mapHeight, int spacing, float lapsePerBlock) =>
        new(mapWidth, mapHeight, spacing, lapsePerBlock);

    internal bool MetadataMatches(int mapWidth, int mapHeight, int spacing, float lapsePerBlock) =>
        this.mapWidth == mapWidth && this.mapHeight == mapHeight && this.spacing == spacing
        && BitConverter.SingleToInt32Bits(this.lapsePerBlock) == BitConverter.SingleToInt32Bits(lapsePerBlock);

    private static void ValidateHeader(ReadOnlySpan<byte> data, int mapWidth, int mapHeight,
        out int spacing, out float lapsePerBlock, out int width, out int height)
    {
        if (data.Length < HeaderSize || data[0] != 'E' || data[1] != 'M' || data[2] != 'C' || data[3] != 'L'
            || BinaryPrimitives.ReadInt32LittleEndian(data[4..]) != 2)
            throw new InvalidDataException("Unsupported Earth climate file; rebuild earthclimate.bin (EMCL v2, spacing 8).");

        spacing = BinaryPrimitives.ReadInt32LittleEndian(data[16..]);
        lapsePerBlock = BinaryPrimitives.ReadSingleLittleEndian(data[20..]);
        if (BinaryPrimitives.ReadInt32LittleEndian(data[8..]) != mapWidth
            || BinaryPrimitives.ReadInt32LittleEndian(data[12..]) != mapHeight
            || spacing != RequiredSpacing || mapWidth <= 0 || mapHeight <= 0
            || !float.IsFinite(lapsePerBlock) || lapsePerBlock > 0)
            throw new InvalidDataException("Earth climate dimensions must match the region store and EMCL v2 spacing must be 8; rebuild earthclimate.bin.");

        width = (mapWidth - 1) / spacing + 1;
        height = (mapHeight - 1) / spacing + 1;
    }

    public bool Contains(int x, int z)
    {
        if ((uint)x >= (uint)mapWidth || (uint)z >= (uint)mapHeight) return false;
        return !tiledClient || HasTile(x / TileWorldSize, z / TileWorldSize);
    }

    public float AnnualTemperature(int x, int z) => Interpolate(x, z, SampleField.MeanTemperature);
    public float VegetationWetness(int x, int z) => Interpolate(x, z, SampleField.Wetness);
    public float WarmestMonthTemperature(int x, int z) => Interpolate(x, z, SampleField.WarmestTemperature);
    public float AnnualPrecipitation(int x, int z) => Interpolate(x, z, SampleField.Precipitation);
    public float AnnualPotentialEvapotranspiration(int x, int z) => Interpolate(x, z, SampleField.PotentialEvapotranspiration);

    internal float ReferenceTerrainY(int x, int z) => Interpolate(x, z, SampleField.ReferenceY);

    /// <summary>
    /// Temperature change at world Y relative to this position's mapped terrain.
    /// CHELSA values describe air at the smoothed terrain surface; air above
    /// that is cooler and below it warmer. Heights are in source (0-255) Y.
    /// </summary>
    public float AltitudeOffset(int x, int z, float sourceY)
    {
        float referenceY = Interpolate(x, z, SampleField.ReferenceY);
        return float.IsNaN(referenceY) ? float.NaN : lapsePerBlock * (sourceY - referenceY);
    }

    public float MonthlyTemperature(int x, int z, double yearFraction)
    {
        if (!TryGetInterpolation(x, z, out Interpolation interpolation)) return float.NaN;
        double monthPosition = ((yearFraction * 12 - 0.5) % 12 + 12) % 12;
        int month = (int)monthPosition;
        int next = (month + 1) % 12;
        float fraction = (float)(monthPosition - month);
        float top = GameMath.Lerp(
            MonthAt(interpolation.Data, interpolation.Offset00, month, next, fraction),
            MonthAt(interpolation.Data, interpolation.Offset10, month, next, fraction), interpolation.Tx);
        float bottom = GameMath.Lerp(
            MonthAt(interpolation.Data, interpolation.Offset01, month, next, fraction),
            MonthAt(interpolation.Data, interpolation.Offset11, month, next, fraction), interpolation.Tx);
        return GameMath.Lerp(top, bottom, interpolation.Tz);
    }

    internal byte[] GetTileBytes(int regionX, int regionZ)
    {
        if (!TileInMap(regionX, regionZ)) throw new ArgumentOutOfRangeException(nameof(regionX));
        Tile tile = GetTile(regionX, regionZ);
        if (tile == null) throw new ObjectDisposedException(nameof(EarthClimate));
        return tile.Data;
    }

    internal bool HasTile(int regionX, int regionZ)
    {
        lock (cacheLock)
        {
            if (!tiles.TryGetValue(TileKey(regionX, regionZ), out Tile tile)) return false;
            Touch(tile);
            return true;
        }
    }

    internal void AddTile(int regionX, int regionZ, byte[] data)
    {
        if (!tiledClient || !TileInMap(regionX, regionZ) || data == null || data.Length != TileByteLength)
            throw new InvalidDataException("Invalid Earth climate tile.");

        lock (cacheLock)
        {
            long key = TileKey(regionX, regionZ);
            if (tiles.TryGetValue(key, out Tile existing))
            {
                existing.Data = data;
                Touch(existing);
                return;
            }

            while (tiles.Count >= maxCachedTiles) RemoveLeastRecentlyUsed();
            var tile = new Tile(regionX, regionZ, data);
            tile.Node = leastRecentlyUsed.AddFirst(tile);
            tiles.Add(key, tile);
        }
    }

    internal void RemoveTile(int regionX, int regionZ)
    {
        lock (cacheLock)
        {
            if (!tiles.Remove(TileKey(regionX, regionZ), out Tile tile)) return;
            leastRecentlyUsed.Remove(tile.Node);
        }
    }

    private float Interpolate(int x, int z, SampleField field)
    {
        if (!TryGetInterpolation(x, z, out Interpolation interpolation)) return float.NaN;
        float top = GameMath.Lerp(
            ReadField(interpolation.Data, interpolation.Offset00, field),
            ReadField(interpolation.Data, interpolation.Offset10, field), interpolation.Tx);
        float bottom = GameMath.Lerp(
            ReadField(interpolation.Data, interpolation.Offset01, field),
            ReadField(interpolation.Data, interpolation.Offset11, field), interpolation.Tx);
        return GameMath.Lerp(top, bottom, interpolation.Tz);
    }

    private bool TryGetInterpolation(int x, int z, out Interpolation result)
    {
        result = default;
        if (tiledClient && ((uint)x >= (uint)mapWidth || (uint)z >= (uint)mapHeight)) return false;
        x = Math.Clamp(x, 0, mapWidth - 1);
        z = Math.Clamp(z, 0, mapHeight - 1);

        if (completeSamples != null)
        {
            float fx = (float)x / spacing;
            float fz = (float)z / spacing;
            int x0 = (int)fx, z0 = (int)fz;
            int x1 = Math.Min(x0 + 1, width - 1), z1 = Math.Min(z0 + 1, height - 1);
            result = new Interpolation(completeSamples,
                HeaderSize + (z0 * width + x0) * SampleSize,
                HeaderSize + (z0 * width + x1) * SampleSize,
                HeaderSize + (z1 * width + x0) * SampleSize,
                HeaderSize + (z1 * width + x1) * SampleSize,
                fx - x0, fz - z0);
            return true;
        }

        int regionX = x / TileWorldSize;
        int regionZ = z / TileWorldSize;
        Tile tile = GetTile(regionX, regionZ);
        if (tile == null) return false;
        float localFx = (float)(x - regionX * TileWorldSize) / spacing;
        float localFz = (float)(z - regionZ * TileWorldSize) / spacing;
        int localX0 = (int)localFx, localZ0 = (int)localFz;
        int localX1 = Math.Min(localX0 + 1, TileSamples - 1);
        int localZ1 = Math.Min(localZ0 + 1, TileSamples - 1);
        result = new Interpolation(tile.Data,
            (localZ0 * TileSamples + localX0) * SampleSize,
            (localZ0 * TileSamples + localX1) * SampleSize,
            (localZ1 * TileSamples + localX0) * SampleSize,
            (localZ1 * TileSamples + localX1) * SampleSize,
            localFx - localX0, localFz - localZ0);
        return true;
    }

    private Tile GetTile(int regionX, int regionZ)
    {
        long key = TileKey(regionX, regionZ);
        lock (cacheLock)
        {
            if (disposed) return null;
            if (tiles.TryGetValue(key, out Tile cached))
            {
                Touch(cached);
                return cached;
            }
            if (tiledClient) return null;
        }

        Tile loaded = new(regionX, regionZ, ReadTile(regionX, regionZ));
        lock (cacheLock)
        {
            if (disposed) return null;
            if (tiles.TryGetValue(key, out Tile raced))
            {
                Touch(raced);
                return raced;
            }
            while (tiles.Count >= maxCachedTiles) RemoveLeastRecentlyUsed();
            loaded.Node = leastRecentlyUsed.AddFirst(loaded);
            tiles.Add(key, loaded);
            return loaded;
        }
    }

    private byte[] ReadTile(int regionX, int regionZ)
    {
        if (fileHandle == null || !TileInMap(regionX, regionZ))
            throw new ArgumentOutOfRangeException(nameof(regionX));

        var data = GC.AllocateUninitializedArray<byte>(TileByteLength);
        int firstSampleX = regionX * TileIntervals;
        int firstSampleZ = regionZ * TileIntervals;
        int validSamplesX = Math.Min(TileSamples, width - firstSampleX);
        int previousSourceZ = -1;
        int rowBytes = TileSamples * SampleSize;
        for (int localZ = 0; localZ < TileSamples; localZ++)
        {
            int sourceZ = Math.Min(firstSampleZ + localZ, height - 1);
            int destinationOffset = localZ * rowBytes;
            if (sourceZ == previousSourceZ)
            {
                data.AsSpan(destinationOffset - rowBytes, rowBytes).CopyTo(data.AsSpan(destinationOffset, rowBytes));
                continue;
            }

            int bytesToRead = validSamplesX * SampleSize;
            long fileOffset = HeaderSize + ((long)sourceZ * width + firstSampleX) * SampleSize;
            ReadExactly(fileHandle, data.AsSpan(destinationOffset, bytesToRead), fileOffset);
            ReadOnlySpan<byte> edgeSample = data.AsSpan(destinationOffset + (validSamplesX - 1) * SampleSize, SampleSize);
            for (int localX = validSamplesX; localX < TileSamples; localX++)
                edgeSample.CopyTo(data.AsSpan(destinationOffset + localX * SampleSize, SampleSize));
            previousSourceZ = sourceZ;
        }
        return data;
    }

    private static void ReadExactly(SafeFileHandle handle, Span<byte> destination, long fileOffset)
    {
        int read = 0;
        while (read < destination.Length)
        {
            int count = RandomAccess.Read(handle, destination[read..], fileOffset + read);
            if (count == 0) throw new EndOfStreamException("Truncated Earth climate file.");
            read += count;
        }
    }

    private bool TileInMap(int regionX, int regionZ) =>
        regionX >= 0 && regionZ >= 0
        && (long)regionX * TileWorldSize < mapWidth
        && (long)regionZ * TileWorldSize < mapHeight;

    private static long TileKey(int regionX, int regionZ) => ((long)(uint)regionX << 32) | (uint)regionZ;

    private void Touch(Tile tile)
    {
        leastRecentlyUsed.Remove(tile.Node);
        leastRecentlyUsed.AddFirst(tile.Node);
    }

    private void RemoveLeastRecentlyUsed()
    {
        Tile tile = leastRecentlyUsed.Last.Value;
        leastRecentlyUsed.RemoveLast();
        tiles.Remove(TileKey(tile.RegionX, tile.RegionZ));
    }

    private static float ReadField(byte[] data, int offset, SampleField field)
    {
        switch (field)
        {
            case SampleField.MeanTemperature:
                float sum = 0;
                for (int month = 0; month < 12; month++)
                    sum += BinaryPrimitives.ReadInt16LittleEndian(data.AsSpan(offset + month * 2, 2));
                return sum / 120f;
            case SampleField.WarmestTemperature:
                short warmest = short.MinValue;
                for (int month = 0; month < 12; month++)
                    warmest = Math.Max(warmest, BinaryPrimitives.ReadInt16LittleEndian(data.AsSpan(offset + month * 2, 2)));
                return warmest * 0.1f;
            case SampleField.Precipitation:
                return BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(offset + 24, 2));
            case SampleField.PotentialEvapotranspiration:
                return BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(offset + 26, 2));
            case SampleField.ReferenceY:
                return BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(offset + 28, 2)) * 0.1f;
            case SampleField.Wetness:
                float precipitation = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(offset + 24, 2));
                float pet = BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan(offset + 26, 2));
                return Math.Min(precipitation / (precipitation + Math.Max(pet, 1f)), Math.Min(1f, precipitation / 200f));
            default:
                throw new ArgumentOutOfRangeException(nameof(field));
        }
    }

    private static float MonthAt(byte[] data, int offset, int month, int next, float fraction)
    {
        float a = BinaryPrimitives.ReadInt16LittleEndian(data.AsSpan(offset + month * 2, 2));
        float b = BinaryPrimitives.ReadInt16LittleEndian(data.AsSpan(offset + next * 2, 2));
        return (a + (b - a) * fraction) * 0.1f;
    }

    public void Dispose()
    {
        lock (cacheLock)
        {
            if (disposed) return;
            disposed = true;
            tiles.Clear();
            leastRecentlyUsed.Clear();
        }
        fileHandle?.Dispose();
    }

    private enum SampleField
    {
        MeanTemperature,
        WarmestTemperature,
        Wetness,
        Precipitation,
        PotentialEvapotranspiration,
        ReferenceY
    }

    private sealed class Tile
    {
        internal readonly int RegionX;
        internal readonly int RegionZ;
        internal byte[] Data;
        internal LinkedListNode<Tile> Node;

        internal Tile(int regionX, int regionZ, byte[] data)
        {
            RegionX = regionX;
            RegionZ = regionZ;
            Data = data;
        }
    }

    private readonly struct Interpolation
    {
        internal readonly byte[] Data;
        internal readonly int Offset00;
        internal readonly int Offset10;
        internal readonly int Offset01;
        internal readonly int Offset11;
        internal readonly float Tx;
        internal readonly float Tz;

        internal Interpolation(byte[] data, int offset00, int offset10, int offset01, int offset11, float tx, float tz)
        {
            Data = data;
            Offset00 = offset00;
            Offset10 = offset10;
            Offset01 = offset01;
            Offset11 = offset11;
            Tx = tx;
            Tz = tz;
        }
    }
}
