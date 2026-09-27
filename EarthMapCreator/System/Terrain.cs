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
        int seaLevel = 92;
        
        int[,] bisectedHeightMap = CutHeightMapForChunk(region, MapPlane.Height, chunkX, chunkZ);
        int[,] bisectedLakeDepthMap = CutHeightMapForChunk(region, MapPlane.LakeDepth, chunkX, chunkZ);
        int[,] bisectedLakeMaskMap = CutHeightMapForChunk(region, MapPlane.LakeMask, chunkX, chunkZ);
        int[,] bisectedLandMaskMap = CutHeightMapForChunk(region, MapPlane.LandMask, chunkX, chunkZ);
        int[,] bisectedOceanBathyMap = CutHeightMapForChunk(region, MapPlane.Bathymetry, chunkX, chunkZ);
        int[,] bisectedRiverMap = CutHeightMapForChunk(region, MapPlane.River, chunkX, chunkZ);
        int[,] bisectedRiverSurfaceMap = CutHeightMapForChunk(region, MapPlane.RiverSurface, chunkX, chunkZ);
        int[,] bisectedRiverDepthMap = CutHeightMapForChunk(region, MapPlane.RiverDepth, chunkX, chunkZ);
        
        // --- Determine max Y for loop boundary ---
        var maxY = int.MinValue;
        for (int lx = 0; lx < chunkSize; lx++)
        {
            for (int lz = 0; lz < chunkSize; lz++)
            {
                bool isLand = bisectedLandMaskMap[lx, lz] > 0;
                bool isLake = bisectedLakeMaskMap[lx, lz] > 0;
                int surfaceHeight;

                if (!isLand && !isLake) { // Ocean surface is always sea level
                    surfaceHeight = seaLevel;
                } else { // Land or lake surface is from the heightmap
                    surfaceHeight = bisectedHeightMap[lx, lz];
                }
                
                if (surfaceHeight > maxY) maxY = surfaceHeight;
            }
        }

        int mapSizeY = _api.WorldManager.MapSizeY;
        
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
                    surfaceHeight = bisectedHeightMap[lx, lz];
                    groundHeight = Math.Max(1, surfaceHeight - bisectedLakeDepthMap[lx, lz]);
                    fluidBlockId = bisectedLakeMaskMap[lx, lz] == salineLakeMask ? saltWater : water;
                }
                else if (!isLand) // Case 2: Ocean
                {
                    groundHeight = bisectedOceanBathyMap[lx, lz] - 1;
                    surfaceHeight = seaLevel;
                    fluidBlockId = saltWater;
                }
                else if (isRiver) // Case 3: River
                {
                    surfaceHeight = bisectedRiverSurfaceMap[lx, lz];
                    int riverDepth = bisectedRiverDepthMap[lx, lz];
                    groundHeight = Math.Max(1, surfaceHeight - riverDepth);
                    fluidBlockId = water;
                }
                else // Case 4: Dry Land
                {
                    groundHeight = bisectedHeightMap[lx, lz];
                    surfaceHeight = groundHeight;
                }

                // Set the engine's heightmaps
                terrainHeightMap[mapIdx] = (ushort)groundHeight;
                rainHeightMap[mapIdx] = (ushort)surfaceHeight;

                // Generate the column based on the determined heights
                for (int yy = 1; yy <= maxY + 1; yy++)
                {
                    if (yy >= mapSizeY) continue;
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