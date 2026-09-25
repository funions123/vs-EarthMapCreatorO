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

        // --- SAFETY CHECK ---
        if (regionX < 0 || regionX >= layers.HeightMap.IntValues.Length || 
            regionZ < 0 || regionZ >= layers.HeightMap.IntValues[0].Length)
        {
            return;
        }

        // Get all required maps for this region
        IntDataMap2D heightMap = layers.HeightMap.IntValues[regionX][regionZ];
        IntDataMap2D lakeDepthMap = layers.LakeDepthMap.IntValues[regionX][regionZ];
        IntDataMap2D lakeMaskMap = layers.LakeMaskMap.IntValues[regionX][regionZ];
        IntDataMap2D landMaskMap = layers.LandMaskMap.IntValues[regionX][regionZ];
        IntDataMap2D oceanBathyMap = layers.OceanBathyMap.IntValues[regionX][regionZ];
        IntDataMap2D riverMap = layers.RiverMap.IntValues[regionX][regionZ];
        IntDataMap2D riverSurfaceMap = layers.RiverSurfaceMap.IntValues[regionX][regionZ];
        IntDataMap2D riverDepthMap = layers.RiverDepthMap.IntValues[regionX][regionZ];
        
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
        int seaLevel = 92;
        
        // Cut maps to chunk size
        int[,] bisectedHeightMap = CutHeightMapForChunk(heightMap, new Vec2i(chunkX, chunkZ), new Vec2i(regionX, regionZ));
        int[,] bisectedLakeDepthMap = CutHeightMapForChunk(lakeDepthMap, new Vec2i(chunkX, chunkZ), new Vec2i(regionX, regionZ));
        int[,] bisectedLakeMaskMap = CutHeightMapForChunk(lakeMaskMap, new Vec2i(chunkX, chunkZ), new Vec2i(regionX, regionZ));
        int[,] bisectedLandMaskMap = CutHeightMapForChunk(landMaskMap, new Vec2i(chunkX, chunkZ), new Vec2i(regionX, regionZ));
        int[,] bisectedOceanBathyMap = CutHeightMapForChunk(oceanBathyMap, new Vec2i(chunkX, chunkZ), new Vec2i(regionX, regionZ));
        int[,] bisectedRiverMap = CutHeightMapForChunk(riverMap, new Vec2i(chunkX, chunkZ), new Vec2i(regionX, regionZ));
        int[,] bisectedRiverSurfaceMap = CutHeightMapForChunk(riverSurfaceMap, new Vec2i(chunkX, chunkZ), new Vec2i(regionX, regionZ));
        int[,] bisectedRiverDepthMap = CutHeightMapForChunk(riverDepthMap, new Vec2i(chunkX, chunkZ), new Vec2i(regionX, regionZ));
        
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
                    fluidBlockId = water;
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
    
    private int[,] CutHeightMapForChunk(IntDataMap2D heightMap, Vec2i chunkCoords, Vec2i regionCoords)
    {
        int chunkSize = _api.WorldManager.ChunkSize;
        int chunkSized2 = chunkSize / 2;
        
        int[,] chunkElevation = new int[chunkSize, chunkSize];

        // top left is least most 
        // e.g. region x/z              1000, 1000
        // topleft chunk x/z            16000,16000 
        // bottom right chunk x/z       16015,16015
        // global top left chunk coordinate of this region
        Vec2i regionTopLeftChunk = new Vec2i(
            chunkSized2 * regionCoords.X,
            chunkSized2 * regionCoords.Y
        );
        
        // subtract 
        Vec2i localChunk = chunkCoords - regionTopLeftChunk;
        
        int maxX = (1+localChunk.X) * chunkSize;
        int maxZ = (1+localChunk.Y) * chunkSize;

        int minX = localChunk.X * chunkSize;
        int minZ = localChunk.Y * chunkSize;
        
        int lx = 0;
        int lz = 0;
        for (int x = minX; x < maxX; x++)
        {
            for (int z = minZ; z < maxZ; z++)
            {
                int height = heightMap.GetInt(x, z);
                chunkElevation[lx, lz] = height;
                lz++;
            }

            lx++;
            lz = 0;
        }
        
        return chunkElevation;
    }

    private bool IsFreshWaterHere(GlobalConfig config, IntDataMap2D riverMap, int x, int z)
    {
        return riverMap.GetInt(x, z) > 0;
    }
}