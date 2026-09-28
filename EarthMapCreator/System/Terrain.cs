using System;
using System.Runtime.CompilerServices;
using Vintagestory.API.Common;
using Vintagestory.API.Datastructures;
using Vintagestory.API.MathTools;
using Vintagestory.API.Server;
using Vintagestory.ServerMods;
using Vintagestory.ServerMods.NoObf;

namespace EarthMapCreator;

public class Terrain : ModSystem
{
    private ICoreServerAPI _api;
    // Fill base rock after vanilla terrain (0) but before rock strata (0.1).
    public override double ExecuteOrder() => 0.05;

    
    public override void StartServerSide(ICoreServerAPI api)
    {
        _api = api;
        api.Event.ChunkColumnGeneration(Event_OnChunkColumnGeneration, EnumWorldGenPass.Terrain, "standard");
    }

    private void Event_OnChunkColumnGeneration(IChunkColumnGenerateRequest request)
    {
        int halfChunkSize = _api.WorldManager.ChunkSize / 2;
        
        int chunkX = request.ChunkX;
        int chunkZ = request.ChunkZ;
        
        int regionX = chunkX / halfChunkSize;
        int regionZ = chunkZ / halfChunkSize;
        
        GenerateTerrain(request, regionX, regionZ);
    }

    protected void GenerateTerrain(IChunkColumnGenerateRequest request, int regionX, int regionZ)
    {
        var layers = EarthMapCreator.Layers;
        int chunkSize = _api.WorldManager.ChunkSize;

        if (!layers.Contains(request.ChunkX * chunkSize, request.ChunkZ * chunkSize))
            return;

        MapRegion region = layers.GetRegion(regionX, regionZ);
        
        // Get chunk data and config
        IServerChunk[] chunks = request.Chunks;
        var chunkX = request.ChunkX;
        var chunkZ = request.ChunkZ;
        var config = GlobalConfig.GetInstance(_api);
        var blockLayerConfig = BlockLayerConfig.GetInstance(_api);
        int bedrock = config.mantleBlockId;
        int rock = config.defaultRockId;
        int water = config.waterBlockId;
        int saltWater = config.saltWaterBlockId;
        const int salineLakeMask = 128; // Geo/pipeline/lakes.py SALINE_LAKE
        int mapSizeY = _api.WorldManager.MapSizeY;
        int seaLevel = RegionStore.WorldY(layers.SeaLevel, mapSizeY);
        
        int[,] bisectedHeightMap = CutHeightMapForChunk(region, MapPlane.Height, chunkX, chunkZ);
        int[,] bisectedLakeDepthMap = CutHeightMapForChunk(region, MapPlane.LakeDepth, chunkX, chunkZ);
        int[,] bisectedLakeMaskMap = CutHeightMapForChunk(region, MapPlane.LakeMask, chunkX, chunkZ);
        int[,] bisectedLandMaskMap = CutHeightMapForChunk(region, MapPlane.LandMask, chunkX, chunkZ);
        int[,] bisectedOceanBathyMap = CutHeightMapForChunk(region, MapPlane.Bathymetry, chunkX, chunkZ);
        int[,] bisectedRiverMap = CutHeightMapForChunk(region, MapPlane.River, chunkX, chunkZ);
        int[,] bisectedRiverSurfaceMap = CutHeightMapForChunk(region, MapPlane.RiverSurface, chunkX, chunkZ);
        int[,] bisectedRiverDepthMap = CutHeightMapForChunk(region, MapPlane.RiverDepth, chunkX, chunkZ);
        int maxY = 0;
        for (int lx = 0; lx < chunkSize; lx++)
        {
            for (int lz = 0; lz < chunkSize; lz++)
            {
                int encoded = bisectedLakeMaskMap[lx, lz] != 0 || bisectedLandMaskMap[lx, lz] != 0
                    ? bisectedHeightMap[lx, lz] : layers.SeaLevel;
                if (bisectedRiverMap[lx, lz] != 0 && bisectedLakeMaskMap[lx, lz] == 0 && bisectedLandMaskMap[lx, lz] != 0)
                    encoded = Math.Max(encoded, bisectedRiverSurfaceMap[lx, lz]);
                maxY = Math.Max(maxY, encoded);
            }
        }
        maxY = Math.Min(mapSizeY - 1, RegionStore.WorldY(maxY, mapSizeY));
        
        ushort[] rainHeightMap = chunks[0].MapChunk.RainHeightMap;
        ushort[] terrainHeightMap = chunks[0].MapChunk.WorldGenTerrainHeightMap;
        
        // Bedrock Layer
        chunks[0].Data.SetBlockBulk(0, chunkSize, chunkSize, bedrock);
        
        // --- Fill layers column by column from bedrock up ---
        for (int lx = 0; lx < chunkSize; lx++)
        {
            for (int lz = 0; lz < chunkSize; lz++)
            {
                int mapIdx = ChunkIndex2d(lx, lz);
                
                bool isLand = bisectedLandMaskMap[lx, lz] > 0;
                bool isLake = bisectedLakeMaskMap[lx, lz] > 0;
                bool isRiver = bisectedRiverMap[lx, lz] > 0;

                int groundHeight;
                int surfaceHeight;
                int fluidBlockId = 0; // 0 means no fluid

                // Determine ground, surface, and fluid type for the current column
                if (isLake) // Case 1: Lake (takes precedence over ocean and river)
                {
                    surfaceHeight = RegionStore.WorldY(bisectedHeightMap[lx, lz], mapSizeY);
                    groundHeight = Math.Max(1, RegionStore.WorldY(Math.Max(1, bisectedHeightMap[lx, lz] - bisectedLakeDepthMap[lx, lz]), mapSizeY));
                    fluidBlockId = bisectedLakeMaskMap[lx, lz] == salineLakeMask ? saltWater : water;
                }
                else if (!isLand) // Case 2: Ocean
                {
                    groundHeight = Math.Max(1, RegionStore.WorldY(Math.Max(1, bisectedOceanBathyMap[lx, lz] - 1), mapSizeY));
                    surfaceHeight = seaLevel;
                    fluidBlockId = saltWater;
                }
                else if (isRiver) // Case 3: River
                {
                    surfaceHeight = RegionStore.WorldY(bisectedRiverSurfaceMap[lx, lz], mapSizeY);
                    int riverDepth = bisectedRiverDepthMap[lx, lz];
                    groundHeight = Math.Max(1, RegionStore.WorldY(Math.Max(1, bisectedRiverSurfaceMap[lx, lz] - riverDepth), mapSizeY));
                    fluidBlockId = water;
                }
                else // Case 4: Dry Land
                {
                    groundHeight = RegionStore.WorldY(bisectedHeightMap[lx, lz], mapSizeY);
                    surfaceHeight = groundHeight;
                }

                // Set the engine's heightmaps
                terrainHeightMap[mapIdx] = (ushort)groundHeight;
                rainHeightMap[mapIdx] = (ushort)surfaceHeight;

                // Clear above carved rivers up to the highest mapped terrain
                // in this chunk without scanning the whole taller world.
                for (int yy = 1; yy <= maxY; yy++)
                {
                    int chunkIndex = yy / chunkSize;
                    if (chunkIndex >= chunks.Length) continue;
                    var chunkData = chunks[chunkIndex].Data;
                    int ly = yy % chunkSize;
                    int chunkIdx = ChunkIndex3d(lx, ly, lz);

                    if (yy <= groundHeight)
                    {
                        chunkData[chunkIdx] = rock;
                    }
                    else if (yy <= surfaceHeight)
                    {
                        // This block will only be entered for ocean or lakes
                        chunkData.SetFluid(chunkIdx, fluidBlockId);
                    }
                    else
                    {
                        chunkData[chunkIdx] = 0; // Air
                    }
                }
            }
        }
    }

    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    private int ChunkIndex3d(int x, int y, int z)
    {
        int chunkSize = _api.WorldManager.ChunkSize;
        return (y * chunkSize + z) * chunkSize + x;
    }
    
    [MethodImpl(MethodImplOptions.AggressiveInlining)]
    private int ChunkIndex2d(int x, int z)
    {
        int chunksize = _api.WorldManager.ChunkSize;
        return z * chunksize + x;
    }
    
    private int[,] CutHeightMapForChunk(MapRegion region, MapPlane plane, int chunkX, int chunkZ)
    {
        int size = _api.WorldManager.ChunkSize;
        int[,] result = new int[size, size];
        int startX = chunkX % (_api.WorldManager.RegionSize / size) * size;
        int startZ = chunkZ % (_api.WorldManager.RegionSize / size) * size;
        for (int x = 0; x < size; x++)
            for (int z = 0; z < size; z++)
                result[x, z] = region.Get(plane, startX + x, startZ + z);
        return result;
    }
}