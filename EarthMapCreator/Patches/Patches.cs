using System;
using System.Reflection;
using HarmonyLib;
using Vintagestory.API.Common;
using Vintagestory.API.Datastructures;
using Vintagestory.API.MathTools;
using Vintagestory.API.Server;
using Vintagestory.ServerMods;

namespace EarthMapCreator.Patches;

// --- Delegates for Private Methods ---
// Define a delegate matching the signature of GenBlockLayers.PutLayers
internal delegate int PutLayersDelegate(GenBlockLayers instance, double posRand, int lx, int lz, int posyoffs, BlockPos pos, IServerChunk[] chunks, float rainRel, float temp, int unscaledTemp, ushort[] heightMap, int biome);

public class EarthMapPatches : ModSystem
{
    private Harmony _patcher;
    
    public static ICoreServerAPI _api; 

    public override void StartServerSide(ICoreServerAPI api)
    {
        _api = api;
        
        Patches.InitAccessors();
        
        _patcher = new Harmony(Mod.Info.ModID);
        _patcher.PatchCategory(Mod.Info.ModID);
    }
    
    public override void AssetsFinalize(ICoreAPI api)
    {
        if (api.Side != EnumAppSide.Server)
        {
            return;
        }
    }

    public override void Dispose()
    {
        _patcher?.UnpatchAll(Mod.Info.ModID);
    }
}

[HarmonyPatchCategory("earthmapcreatoro")]
internal static class Patches
{
    // Private method delegate, bound once when the mod starts.
    private static PutLayersDelegate PutLayers;
    
    public static void InitAccessors()
    {
        Type gblType = typeof(GenBlockLayers);
        
        MethodInfo putLayersMethod = AccessTools.Method(gblType, "PutLayers", new Type[] { typeof(double), typeof(int), typeof(int), typeof(int), typeof(BlockPos), typeof(IServerChunk[]), typeof(float), typeof(float), typeof(int), typeof(ushort[]), typeof(int) });
        PutLayers = (PutLayersDelegate)Delegate.CreateDelegate(typeof(PutLayersDelegate), putLayersMethod);
    }


    [HarmonyPrefix]
    [HarmonyPatch(typeof(GenBlockLayers), "OnChunkColumnGeneration", new Type[] { typeof(IChunkColumnGenerateRequest) })]
    public static bool GenBlockLayers_OnChunkColumnGen_Prefix(GenBlockLayers __instance, IChunkColumnGenerateRequest request)
    {
        var api = EarthMapPatches._api;
        var mapheight = api.WorldManager.MapSizeY;
        var chunksize = api.WorldManager.ChunkSize;
        
        // --- Core Logic ---
        var chunks = request.Chunks;
        int chunkX = request.ChunkX;
        int chunkZ = request.ChunkZ;

        // Your patched OnChunkColumnGeneration still requires the climate map data
        IntDataMap2D forestMap = chunks[0].MapChunk.MapRegion.ForestMap;
        IntDataMap2D climateMap = chunks[0].MapChunk.MapRegion.ClimateMap;
        IntDataMap2D biomeMap = chunks[0].MapChunk.MapRegion.BiomeMap;
        
        ushort[] heightMap = chunks[0].MapChunk.RainHeightMap;

        int regionChunkSize = api.WorldManager.RegionSize / chunksize;
        int rdx = chunkX % regionChunkSize;
        int rdz = chunkZ % regionChunkSize;

        // Amount of data points per chunk
        float climateStep = (float)climateMap.InnerSize / regionChunkSize;
        float forestStep = (float)forestMap.InnerSize / regionChunkSize;
        float biomeStep = biomeMap == null ? 0 : (float)biomeMap.InnerSize / regionChunkSize;

        // Retrieves the map data on the chunk edges
        int forestUpLeft = forestMap.GetUnpaddedInt((int)(rdx * forestStep), (int)(rdz * forestStep));
        int forestUpRight = forestMap.GetUnpaddedInt((int)(rdx * forestStep + forestStep), (int)(rdz * forestStep));
        int forestBotLeft = forestMap.GetUnpaddedInt((int)(rdx * forestStep), (int)(rdz * forestStep + forestStep));
        int forestBotRight = forestMap.GetUnpaddedInt((int)(rdx * forestStep + forestStep), (int)(rdz * forestStep + forestStep));

        // increasing x -> left to right
        // increasing z -> top to bottom
        float transitionSize = __instance.blockLayerConfig.blockLayerTransitionSize;
        BlockPos herePos = new BlockPos(0);


        for (int x = 0; x < chunksize; x++)
        {
            for (int z = 0; z < chunksize; z++)
            {
                int biome = biomeMap == null ? 0 : biomeMap.GetUnpaddedInt(
                    (int)(rdx * biomeStep + (float)x / chunksize * biomeStep),
                    (int)(rdz * biomeStep + (float)z / chunksize * biomeStep));
                herePos.Set(chunkX * chunksize + x, 1, chunkZ * chunksize + z);
                
                // Keep posRand for transitionRand calculation, removed climate jittering call
                double posRand = (double)GameMath.MurmurHash3(herePos.X, 1, herePos.Z) / int.MaxValue;
                double transitionRand = (posRand + 1) * transitionSize;

                int posY = heightMap[z * chunksize + x];
                if (posY >= mapheight) continue;

                // Removed climate jittering from GetUnpaddedColorLerped
                int climate = climateMap.GetUnpaddedColorLerped(
                    rdx * climateStep + climateStep * (float)x / chunksize,
                    rdz * climateStep + climateStep * (float)z / chunksize
                );

                int tempUnscaled = (climate >> 16) & 0xff;
                int rainUnscaled = (climate >> 8) & 0xff; // Extract rain (Green channel)
                
                // Convert 0-255 temp (Red channel) back to -30f to 40f range (70f total span)
                float temp = ((float)tempUnscaled / 255f) * 70f - 30f; 
                
                // Convert 0-255 temp (Red channel) to 0.0-1.0 relative value
                float tempRel = (float)tempUnscaled / 255f;
                
                // Convert 0-255 rain (Green channel) to 0.0-1.0 relative value
                float rainRel = (float)rainUnscaled / 255f;
                
                float forestRel = GameMath.BiLerp(forestUpLeft, forestUpRight, forestBotLeft, forestBotRight, (float)x / chunksize, (float)z / chunksize) / 255f;

                int rocky = chunks[0].MapChunk.WorldGenTerrainHeightMap[z * chunksize + x];
                int chunkY = rocky / chunksize;
                int lY = rocky % chunksize;
                int index3d = (chunksize * lY + z) * chunksize + x;

                int rockblockID = chunks[chunkY].Data.GetBlockIdUnsafe(index3d);
                var hereblock = api.World.Blocks[rockblockID];
                if (hereblock.BlockMaterial != EnumBlockMaterial.Stone && hereblock.BlockMaterial != EnumBlockMaterial.Water)
                {
                    continue;
                }

                herePos.Y = posY;
                int disty = (int)(__instance.distort2dx.Noise(-herePos.X, -herePos.Z) / 4.0);
                PutLayers(__instance, transitionRand, x, z, disty, herePos, chunks, rainRel, temp, tempUnscaled, heightMap, biome);
                __instance.PlaceTallGrass(x, posY, z, chunks, rainRel, tempRel, temp, forestRel, biome);
            }
        }
        
        return false; // Skip the original function
    }
    
    [HarmonyPrefix]
    [HarmonyPatch(typeof(GenTerra), "OnChunkColumnGen", new Type[] { typeof(IChunkColumnGenerateRequest) })]
    public static bool GenTerra_OnChunkColumnGen_Prefix(GenTerra __instance, IChunkColumnGenerateRequest request)
    {
        return false;
    }
    
    [HarmonyPrefix]
    [HarmonyPatch(typeof(GenMaps), "GetClimateMapGen")]
    public static bool GetClimateMapGen_Prefix(long seed, NoiseClimate climateNoise, ref MapLayerBase __result)
    {
        var sapi = EarthMapPatches._api;
        if (sapi == null) return true; 

        sapi.Logger.Notification("[EarthMapCreator] Harmony patch triggered: Overwriting GetClimateMapGen.");

        __result = new MapLayerFromImage(seed, EarthMapCreator.Layers, MapPlane.Climate, sapi, TerraGenConfig.climateMapScale, Climate.ClimatePostProcess);
        
        return false; // Skip the original method
    }
    
    [HarmonyPrefix]
    [HarmonyPatch(typeof(GenMaps), "GetForestMapGen")]
    public static bool GetForestMapGen_Prefix(long seed, int scale, ref MapLayerBase __result)
    {
        var sapi = EarthMapPatches._api;
        if (sapi == null || seed != sapi.WorldManager.Seed + 2 || scale != TerraGenConfig.forestMapScale)
        {
            return true; // Shrub and biome generators must retain their vanilla data.
        }

        sapi.Logger.Notification("[EarthMapCreator] Harmony patch triggered: Overwriting GetForestMapGen.");

        __result = new MapLayerFromImage(seed, EarthMapCreator.Layers, MapPlane.Tree, sapi, scale, Climate.ForestPostProcess);
        
        return false; // Skip the original method
    }
    
}