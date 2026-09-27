using Vintagestory.API.Server;
using Vintagestory.ServerMods;

namespace EarthMapCreator;

/// <summary>Serves a map layer from bounded region-backed bytes.</summary>
public class MapLayerFromImage : MapLayerBase
{
    private readonly RegionStore source;
    private readonly MapPlane plane;
    private readonly int scale;
    private readonly System.Func<int, int, int, int> postProcess;

    public MapLayerFromImage(long seed, RegionStore source, MapPlane plane, ICoreServerAPI api, int scale, System.Func<int, int, int, int> postProcess = null) : base(seed)
    {
        this.source = source;
        this.plane = plane;
        this.scale = scale;
        this.postProcess = postProcess;
    }

    public override int[] GenLayer(int xCoord, int zCoord, int sizeX, int sizeZ)
    {
        int[] result = new int[sizeX * sizeZ];
        
        int blockXStart = xCoord * scale;
        int blockZStart = zCoord * scale;

        for (int x = 0; x < sizeX; x++)
        {
            for (int z = 0; z < sizeZ; z++)
            {
                // Calculate the final block coordinates for the current point in the layer.
                int currentBlockX = blockXStart + x * scale;
                int currentBlockZ = blockZStart + z * scale;
                if (!source.Contains(currentBlockX, currentBlockZ))
                {
                    result[z * sizeX + x] = 0;
                    continue;
                }

                int rawValue = source.Get(plane, currentBlockX, currentBlockZ);
                result[z * sizeX + x] = postProcess != null
                    ? postProcess(rawValue, currentBlockX, currentBlockZ)
                    : rawValue;
            }
        }

        return result;
    }
}
