using System;
using System.Collections.Generic;
using System.IO;
using Microsoft.Win32.SafeHandles;

namespace EarthMapCreator;

public enum MapPlane
{
    Height, LakeDepth, Bathymetry, Tree, River, RiverSurface,
    RiverDepth, LakeMask, LandMask
}

public sealed class MapRegion
{
    private const int Size = 512;
    private readonly byte[] data;

    internal MapRegion(byte[] data) => this.data = data;

    public int Get(MapPlane plane, int x, int z) =>
        data[(int)plane * Size * Size + z * Size + x];
}

public sealed class RegionStore : IDisposable
{
    private const int Size = 512;
    private const int PlaneCount = 9;
    private const int RegionBytes = Size * Size * PlaneCount;
    private const int HeaderBytes = 28;
    private const int Capacity = 64; // 160 MiB of source map data
    private readonly SafeFileHandle file;
    private readonly int zRegions;
    private readonly Dictionary<(int x, int z), LinkedListNode<((int x, int z) key, MapRegion region)>> cached = new();
    private readonly LinkedList<((int x, int z) key, MapRegion region)> recent = new();
    private readonly object gate = new();

    public int Width { get; }
    public int Height { get; }
    public int SeaLevel { get; }
    // The stored byte Y values describe a 256-high world. Stretch absolute
    // positions at runtime; 0 remains 0 and the highest encoded Y becomes
    // the highest block of the selected world height.
    public static int WorldY(int encodedY, int mapSizeY) =>
        (int)(((long)encodedY * (mapSizeY - 1) + 127) / 255);

    public RegionStore(string path)
    {
        file = File.OpenHandle(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        try
        {
            Span<byte> header = stackalloc byte[HeaderBytes];
            ReadFully(header, 0);
            if (!header[..8].SequenceEqual("EMREGION"u8) ||
                BitConverter.ToInt32(header[8..12]) != 3 ||
                BitConverter.ToInt32(header[20..24]) != Size)
                throw new InvalidDataException("Unsupported Earth map region format");
            Width = BitConverter.ToInt32(header[12..16]);
            Height = BitConverter.ToInt32(header[16..20]);
            SeaLevel = BitConverter.ToInt32(header[24..28]);
            if (Width <= 0 || Height <= 0 || Width % Size != 0 || Height % Size != 0 ||
                SeaLevel <= 1 || SeaLevel >= 255 ||
                RandomAccess.GetLength(file) != HeaderBytes + (long)Width / Size * (Height / Size) * RegionBytes)
                throw new InvalidDataException($"Invalid Earth map dimensions or length: {Width}x{Height}");
            zRegions = Height / Size;
        }
        catch
        {
            file.Dispose();
            throw;
        }
    }

    private void ReadFully(Span<byte> buffer, long offset)
    {
        while (!buffer.IsEmpty)
        {
            int count = RandomAccess.Read(file, buffer, offset);
            if (count == 0) throw new EndOfStreamException("Earth map region data is truncated");
            buffer = buffer[count..];
            offset += count;
        }
    }

    public bool Contains(int x, int z) => x >= 0 && z >= 0 && x < Width && z < Height;

    public MapRegion GetRegion(int x, int z)
    {
        if (x < 0 || z < 0 || x >= Width / Size || z >= zRegions)
            throw new ArgumentOutOfRangeException(nameof(x), $"Region {x},{z} lies outside the Earth map");
        lock (gate)
        {
            var key = (x, z);
            if (cached.TryGetValue(key, out var node))
            {
                recent.Remove(node);
                recent.AddFirst(node);
                return node.Value.region;
            }
            var bytes = new byte[RegionBytes];
            long offset = HeaderBytes + ((long)x * zRegions + z) * RegionBytes;
            ReadFully(bytes, offset);
            var region = new MapRegion(bytes);
            var added = recent.AddFirst((key, region));
            cached.Add(key, added);
            if (cached.Count > Capacity)
            {
                var oldest = recent.Last!;
                cached.Remove(oldest.Value.key);
                recent.RemoveLast();
            }
            // A returned region remains valid even after cache eviction.
            return region;
        }
    }

    public int Get(MapPlane plane, int x, int z)
    {
        if (!Contains(x, z)) throw new ArgumentOutOfRangeException(nameof(x));
        return GetRegion(x / Size, z / Size).Get(plane, x % Size, z % Size);
    }

    public void Dispose()
    {
        lock (gate)
        {
            cached.Clear();
            recent.Clear();
            file.Dispose();
        }
    }
}
