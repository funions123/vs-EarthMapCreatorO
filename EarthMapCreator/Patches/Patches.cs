using System;
using System.Collections.Generic;
using System.Reflection;
using HarmonyLib;
using Vintagestory.API.Client;
using Vintagestory.API.Common;
using Vintagestory.API.Datastructures;
using Vintagestory.API.MathTools;
using Vintagestory.API.Server;
using Vintagestory.ServerMods;
using Vintagestory.GameContent;

namespace EarthMapCreator.Patches;

// --- Delegates for Private Methods ---
// Define a delegate matching the signature of GenBlockLayers.PutLayers
internal delegate int PutLayersDelegate(GenBlockLayers instance, double posRand, int lx, int lz, int posyoffs, BlockPos pos, IServerChunk[] chunks, float rainRel, float temp, int unscaledTemp, ushort[] heightMap, int biome);

public class EarthMapPatches : ModSystem
{
    private Harmony _serverPatcher;
    private static Harmony climatePatcher;
    private static int climateUsers;
    public static ICoreServerAPI _api;
    internal static ICoreClientAPI ClientApi;

    public override void Start(ICoreAPI api)
    {
        if (climateUsers++ == 0)
        {
            climatePatcher = new Harmony(Mod.Info.ModID + ".climate");
            climatePatcher.Patch(AccessTools.Method(typeof(ModTemperature), "Event_OnGetClimate"),
                prefix: new HarmonyMethod(AccessTools.Method(typeof(Patches), nameof(Patches.UpdateMonthlyTemperature))));
            climatePatcher.Patch(AccessTools.Method(typeof(Vintagestory.Client.NoObf.ClientWorldMap), "GetClimateAt",
                    new[] { typeof(BlockPos), typeof(EnumGetClimateMode), typeof(double) }),
                postfix: new HarmonyMethod(AccessTools.Method(typeof(Patches), nameof(Patches.ClientClimateTemperature))));
            climatePatcher.Patch(AccessTools.Method(typeof(Vintagestory.Client.NoObf.ClientWorldMap), "getColorMapData"),
                postfix: new HarmonyMethod(AccessTools.Method(typeof(Patches), nameof(Patches.ClientColorMapTemperature))));
            climatePatcher.Patch(AccessTools.Method(typeof(Vintagestory.Client.NoObf.ClientWorldMap), "ApplyColorMapOnRgba",
                    new[] { typeof(ColorMap), typeof(ColorMap), typeof(int), typeof(int), typeof(int), typeof(int), typeof(bool) }),
                prefix: new HarmonyMethod(AccessTools.Method(typeof(Patches), nameof(Patches.ClientPositionColorMap))));
        }
    }

    public override void StartClientSide(ICoreClientAPI api) => ClientApi = api;

    public override void StartServerSide(ICoreServerAPI api)
    {
        _api = api;
        Patches.InitAccessors();
        _serverPatcher = new Harmony(Mod.Info.ModID + ".server");
        _serverPatcher.PatchCategory(Mod.Info.ModID);
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
        _serverPatcher?.UnpatchAll(Mod.Info.ModID + ".server");
        if (_serverPatcher != null) _api = null;
        if (--climateUsers == 0) climatePatcher.UnpatchAll(Mod.Info.ModID + ".climate");
        if (ClientApi != null) ClientApi = null;
    }
}

[HarmonyPatchCategory("earthmapcreatoro")]
internal static class Patches
{
    // Private method delegate, bound once when the mod starts.
    private static PutLayersDelegate PutLayers;
    private static readonly string[] LowSoilCodes = { "soil-low-none", "soil-low-verysparse", "soil-low-sparse", "soil-low-normal" };
    private static readonly string[] MediumSoilCodes = { "soil-medium-none", "soil-medium-verysparse", "soil-medium-sparse", "soil-medium-normal" };
    private static readonly int[] lowSoilIds = new int[4];
    private static readonly int[] mediumSoilIds = new int[4];
    private const int RiverbankHaloRadius = 4;
    [ThreadStatic] private static float? LayerTemperature;

    // CHELSA temperatures already include altitude. While this mod places Earth
    // block layers, vanilla must not cool deeper layers by their Y below sea level.
    [HarmonyTranspiler]
    [HarmonyPatch(typeof(GenBlockLayers), "LoadBlockLayers")]
    public static IEnumerable<CodeInstruction> FixedLayerTemperature(IEnumerable<CodeInstruction> instructions)
    {
        MethodInfo vanilla = AccessTools.Method(typeof(Vintagestory.API.Common.Climate), nameof(Vintagestory.API.Common.Climate.GetScaledAdjustedTemperatureFloat));
        MethodInfo replacement = AccessTools.Method(typeof(Patches), nameof(LayerTemperatureFor));
        int replaced = 0;
        foreach (CodeInstruction instruction in instructions)
        {
            if (instruction.Calls(vanilla))
            {
                instruction.operand = replacement;
                replaced++;
            }
            yield return instruction;
        }
        if (replaced != 1) throw new InvalidOperationException($"Expected one layer temperature call, found {replaced}");
    }

    private static float LayerTemperatureFor(int unscaledTemp, int distToSeaLevel) =>
        LayerTemperature ?? Vintagestory.API.Common.Climate.GetScaledAdjustedTemperatureFloat(unscaledTemp, distToSeaLevel);

    // The packed climate map contains CHELSA's terrain-level temperature.
    // GenSnowLayer must not apply vanilla's second altitude correction.
    [HarmonyTranspiler]
    [HarmonyPatch(typeof(GenSnowLayer), "OnChunkColumnGen")]
    public static IEnumerable<CodeInstruction> SnowLayerSurfaceTemperature(IEnumerable<CodeInstruction> instructions)
    {
        MethodInfo vanilla = AccessTools.Method(typeof(Vintagestory.API.Common.Climate), nameof(Vintagestory.API.Common.Climate.GetScaledAdjustedTemperatureFloat));
        MethodInfo replacement = AccessTools.Method(typeof(Patches), nameof(SnowSurfaceTemperature));
        int replaced = 0;
        foreach (CodeInstruction instruction in instructions)
        {
            if (instruction.Calls(vanilla))
            {
                instruction.operand = replacement;
                replaced++;
            }
            yield return instruction;
        }
        if (replaced != 1) throw new InvalidOperationException($"Expected one snow-layer temperature call, found {replaced}");
    }

    private static float SnowSurfaceTemperature(int unscaledTemp, int distToSeaLevel) =>
        Vintagestory.API.Common.Climate.GetScaledAdjustedTemperatureFloat(unscaledTemp, 0);

    // BlockPatchConfig tests temperature against a packed climate map that
    // already represents the local terrain; its vanilla height correction
    // otherwise excludes loose stones from high, mild steppe surfaces.
    [HarmonyTranspiler]
    [HarmonyPatch(typeof(Vintagestory.ServerMods.NoObf.BlockPatchConfig), "IsPatchSuitableAt")]
    public static IEnumerable<CodeInstruction> PatchSurfaceTemperature(IEnumerable<CodeInstruction> instructions)
    {
        MethodInfo vanilla = AccessTools.Method(typeof(Vintagestory.API.Common.Climate), nameof(Vintagestory.API.Common.Climate.GetScaledAdjustedTemperature));
        MethodInfo replacement = AccessTools.Method(typeof(Patches), nameof(PatchSurfaceTemperatureFor));
        int replaced = 0;
        foreach (CodeInstruction instruction in instructions)
        {
            if (instruction.Calls(vanilla))
            {
                instruction.operand = replacement;
                replaced++;
            }
            yield return instruction;
        }
        if (replaced != 1) throw new InvalidOperationException($"Expected one block-patch temperature call, found {replaced}");
    }

    private static int PatchSurfaceTemperatureFor(int unscaledTemp, int distToSeaLevel) =>
        Vintagestory.API.Common.Climate.GetScaledAdjustedTemperature(unscaledTemp, 0);

    // The packed climate map is already at local terrain temperature. Vanilla
    // interprets its byte as sea-level air and cools it a second time.
    [HarmonyTranspiler]
    [HarmonyPatch(typeof(WgenTreeSupplier), "GetRandomGenForClimate")]
    public static IEnumerable<CodeInstruction> TreeTemperature(IEnumerable<CodeInstruction> instructions) => SurfaceIntegerTemperature(instructions);

    [HarmonyTranspiler]
    [HarmonyPatch(typeof(WorldGenStructure), "TryGenerate")]
    public static IEnumerable<CodeInstruction> StructureTemperature(IEnumerable<CodeInstruction> instructions) => SurfaceIntegerTemperature(instructions);

    private static IEnumerable<CodeInstruction> SurfaceIntegerTemperature(IEnumerable<CodeInstruction> instructions) =>
        ReplaceTemperatureAdjustment(instructions,
            AccessTools.Method(typeof(Vintagestory.API.Common.Climate), nameof(Vintagestory.API.Common.Climate.GetScaledAdjustedTemperature)),
            AccessTools.Method(typeof(Patches), nameof(PatchSurfaceTemperatureFor)));

    [HarmonyTranspiler]
    [HarmonyPatch(typeof(GenDeposits), "GenDeposit")]
    public static IEnumerable<CodeInstruction> DepositTemperature(IEnumerable<CodeInstruction> instructions) => SurfaceFloatTemperature(instructions);

    [HarmonyTranspiler]
    [HarmonyPatch(typeof(GenPonds), "TryPlacePondAt")]
    public static IEnumerable<CodeInstruction> PondTemperature(IEnumerable<CodeInstruction> instructions) => SurfaceFloatTemperature(instructions);

    [HarmonyTranspiler]
    [HarmonyPatch(typeof(GenCreatures), "TrySpawnGroupAt")]
    public static IEnumerable<CodeInstruction> CreatureTemperature(IEnumerable<CodeInstruction> instructions) => SurfaceFloatTemperature(instructions);

    [HarmonyTranspiler]
    [HarmonyPatch(typeof(BlockSchematicStructure), "GetBlockLayerBlock")]
    public static IEnumerable<CodeInstruction> SchematicTemperature(IEnumerable<CodeInstruction> instructions) => SurfaceFloatTemperature(instructions);

    private static IEnumerable<CodeInstruction> SurfaceFloatTemperature(IEnumerable<CodeInstruction> instructions) =>
        ReplaceTemperatureAdjustment(instructions,
            AccessTools.Method(typeof(Vintagestory.API.Common.Climate), nameof(Vintagestory.API.Common.Climate.GetScaledAdjustedTemperatureFloat)),
            AccessTools.Method(typeof(Patches), nameof(SnowSurfaceTemperature)));

    private static IEnumerable<CodeInstruction> ReplaceTemperatureAdjustment(IEnumerable<CodeInstruction> instructions, MethodInfo vanilla, MethodInfo replacement)
    {
        int replaced = 0;
        foreach (CodeInstruction instruction in instructions)
        {
            if (instruction.Calls(vanilla))
            {
                instruction.operand = replacement;
                replaced++;
            }
            yield return instruction;
        }
        if (replaced != 1) throw new InvalidOperationException($"Expected one call to {vanilla}, found {replaced}");
    }

    // The game accessor applies a vanilla sea-level lapse before consumers see
    // WorldGenTemperature. CHELSA's cell is instead referenced to local terrain.
    [HarmonyPostfix]
    [HarmonyPatch(typeof(Vintagestory.Server.ServerWorldMap), "getWorldGenClimateAt")]
    public static void ServerClimateTemperature(BlockPos pos, ref ClimateCondition __result)
    {
        if (__result == null) return;
        EarthClimate earth = EarthMapCreator.ClimateData;
        if (earth == null || !earth.Contains(pos.X, pos.Z)) return;
        float temperature = TerrainTemperature(earth, pos.X, pos.Y, pos.Z, EarthMapPatches._api.World.BlockAccessor.MapSizeY);
        __result.WorldGenTemperature = temperature;
        __result.Temperature = temperature;
    }

    public static void ClientClimateTemperature(Vintagestory.Client.NoObf.ClientWorldMap __instance, BlockPos pos, EnumGetClimateMode mode, ref ClimateCondition __result)
    {
        if (__result == null) return;
        EarthClimate earth = EarthMapCreator.ClientClimateData;
        if (earth == null || !earth.Contains(pos.X, pos.Z)) return;
        float temperature = TerrainTemperature(earth, pos.X, pos.Y, pos.Z, __instance.MapSizeY);
        __result.WorldGenTemperature = temperature;
        if (mode == EnumGetClimateMode.WorldGenValues) __result.Temperature = temperature;
    }


    public static void ClientColorMapTemperature(Vintagestory.Client.NoObf.ClientWorldMap __instance,
        int posX, int posY, int posZ, ref Vintagestory.API.Client.ColorMapData __result)
    {
        EarthClimate earth = EarthMapCreator.ClientClimateData;
        if (earth == null || !earth.Contains(posX, posZ)) return;
        int temp = Vintagestory.API.Common.Climate.DescaleTemperature(
            TerrainTemperature(earth, posX, posY, posZ, __instance.MapSizeY));
        __result.Value = (__result.Value & ~(0xFF << 16)) | (temp << 16);
    }

    public static bool ClientPositionColorMap(Vintagestory.Client.NoObf.ClientWorldMap __instance,
        ColorMap climateMap, ColorMap seasonMap,
        int color, int posX, int posY, int posZ, bool flipRb, ref int __result)
    {
        EarthClimate earth = EarthMapCreator.ClientClimateData;
        if (earth == null || !earth.Contains(posX, posZ)) return true;
        int temp = Vintagestory.API.Common.Climate.DescaleTemperature(
            TerrainTemperature(earth, posX, posY, posZ, __instance.MapSizeY));
        int noiseX = GameMath.MurmurHash3Mod(posX, 0, posZ, 3);
        int noiseZ = GameMath.MurmurHash3Mod(posX, 1, posZ, 3);
        int climate = __instance.GetClimate(posX + noiseX, posZ + noiseZ);
        int rain = Vintagestory.API.Common.Climate.GetRainFall((climate >> 8) & 0xFF, posY);
        __result = __instance.ApplyColorMapOnRgba(climateMap, seasonMap, color, rain, temp,
            flipRb, (float)GameMath.MurmurHash3Mod(posX, posY, posZ, 100) / 100f,
            EarthMapPatches.ClientApi.World.Calendar.GetHemisphere(new BlockPos(posX, posY, posZ)) == EnumHemisphere.South ? 0.5f : 0f, 0);
        return false;
    }

    private static float TerrainTemperature(EarthClimate earth, int x, int y, int z, int mapSizeY) =>
        earth.AnnualTemperature(x, z) + earth.AltitudeOffset(x, z, y * 255f / (mapSizeY - 1));


    
    public static void InitAccessors()
    {
        Type gblType = typeof(GenBlockLayers);
        
        MethodInfo putLayersMethod = AccessTools.Method(gblType, "PutLayers", new Type[] { typeof(double), typeof(int), typeof(int), typeof(int), typeof(BlockPos), typeof(IServerChunk[]), typeof(float), typeof(float), typeof(int), typeof(ushort[]), typeof(int) });
        var blocks = EarthMapPatches._api.World;
        for (int coverage = 0; coverage < LowSoilCodes.Length; coverage++)
        {
            lowSoilIds[coverage] = blocks.GetBlock(new AssetLocation("game", LowSoilCodes[coverage])).Id;
            mediumSoilIds[coverage] = blocks.GetBlock(new AssetLocation("game", MediumSoilCodes[coverage])).Id;
        }
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
        IntDataMap2D biomeMap = chunks[0].MapChunk.MapRegion.BiomeMap;
        
        RegionStore layers = EarthMapCreator.Layers;
        ushort[] heightMap = chunks[0].MapChunk.RainHeightMap;

        int regionChunkSize = api.WorldManager.RegionSize / chunksize;
        int rdx = chunkX % regionChunkSize;
        int rdz = chunkZ % regionChunkSize;

        // Amount of data points per chunk
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

                EarthClimate earth = EarthMapCreator.ClimateData;
                float annualTemp = earth.AnnualTemperature(herePos.X, herePos.Z);
                float warmest = earth.WarmestMonthTemperature(herePos.X, herePos.Z);
                float rainRel = earth.VegetationWetness(herePos.X, herePos.Z);
                int tempUnscaled = Vintagestory.API.Common.Climate.DescaleTemperature(annualTemp);
                float tempRel = tempUnscaled / 255f;
                // Only block layers use this classification; weather retains the real climate.
                // Snowpack sites use -20 °C, selecting full snow blocks over gravel.
                float temp = warmest < EarthMapCreator.config.SnowpackWarmestMonthTemperature ? -20f : Math.Max(annualTemp, -10f);
                if (warmest < EarthClimate.GrassGrowingSeasonTemperature || (annualTemp < 10f && rainRel < 0.19f))
                    rainRel = 0f;
                
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
                LayerTemperature = temp;
                try
                {
                    PutLayers(__instance, transitionRand, x, z, disty, herePos, chunks, rainRel, temp, tempUnscaled, heightMap, biome);
                }
                finally
                {
                    LayerTemperature = null;
                }
                if (layers.Contains(herePos.X, herePos.Z) && rainRel >= 0.19f && warmest >= EarthClimate.GrassGrowingSeasonTemperature &&
                    layers.Get(MapPlane.LandMask, herePos.X, herePos.Z) != 0 &&
                    layers.Get(MapPlane.LakeMask, herePos.X, herePos.Z) == 0 &&
                    layers.Get(MapPlane.River, herePos.X, herePos.Z) == 0)
                {
                    int surfaceY = chunks[0].MapChunk.WorldGenTerrainHeightMap[z * chunksize + x];
                    int surfaceIndex = (chunksize * (surfaceY % chunksize) + z) * chunksize + x;
                    var surfaceData = chunks[surfaceY / chunksize].Data;
                    int surfaceBlockId = surfaceData.GetBlockIdUnsafe(surfaceIndex);
                    for (int coverage = 0; coverage < lowSoilIds.Length; coverage++)
                    {
                        if (surfaceBlockId != lowSoilIds[coverage]) continue;
                        if (IsNearRiver(layers, herePos.X, herePos.Z))
                            surfaceData[surfaceIndex] = mediumSoilIds[coverage];
                        break;
                    }
                }
                if (warmest >= EarthClimate.GrassGrowingSeasonTemperature)
                    __instance.PlaceTallGrass(x, posY, z, chunks, rainRel, tempRel, temp, forestRel, biome);
            }
        }
        
        return false; // Skip the original function
    }
    
    private static bool IsNearRiver(RegionStore layers, int x, int z)
    {
        for (int dz = -RiverbankHaloRadius; dz <= RiverbankHaloRadius; dz++)
        {
            for (int dx = -RiverbankHaloRadius; dx <= RiverbankHaloRadius; dx++)
            {
                if (dx * dx + dz * dz > RiverbankHaloRadius * RiverbankHaloRadius) continue;
                int rx = x + dx, rz = z + dz;
                if (layers.Contains(rx, rz) && layers.Get(MapPlane.River, rx, rz) != 0) return true;
            }
        }
        return false;
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

        __result = new MapLayerFromClimate(seed, EarthMapCreator.ClimateData, TerraGenConfig.climateMapScale);
        
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

        __result = new MapLayerFromImage(seed, EarthMapCreator.Layers, MapPlane.Tree, sapi, scale, ForestMapProcessor.ForestPostProcess);
        
        return false; // Skip the original method
    }
    
    [HarmonyPrefix]
    [HarmonyPatch(typeof(WeatherSystemBase), "GetRainCloudness")]
    public static void WeatherPrecipitation_Prefix(ClimateCondition conds, double posX, double posZ, out float? __state)
    {
        __state = null;
        EarthClimate earth = EarthMapCreator.ClimateData;
        int x = (int)Math.Floor(posX), z = (int)Math.Floor(posZ);
        if (conds == null || earth == null || !earth.Contains(x, z)) return;
        __state = conds.Rainfall;
        float p = earth.AnnualPrecipitation(x, z);
        conds.Rainfall = p / (p + 800f);
    }

    [HarmonyPostfix]
    [HarmonyPatch(typeof(WeatherSystemBase), "GetRainCloudness")]
    public static void WeatherPrecipitation_Postfix(ClimateCondition conds, float? __state)
    {
        if (__state.HasValue) conds.Rainfall = __state.Value;
    }

    [HarmonyPrefix]
    [HarmonyPatch(typeof(WeatherSimulationRegion), "RandomWeatherEvent")]
    public static void WeatherEvent_Prefix(WeatherSimulationRegion __instance, out float? __state)
    {
        __state = null;
        ClimateCondition conditions = __instance.weatherData.climateCond;
        EarthClimate earth = EarthMapCreator.ClimateData;
        int x = __instance.regionX * 512 + 256;
        int z = __instance.regionZ * 512 + 256;
        if (conditions == null || earth == null || !earth.Contains(x, z)) return;
        __state = conditions.WorldgenRainfall;
        float p = earth.AnnualPrecipitation(x, z);
        conditions.WorldgenRainfall = p / (p + 800f);
    }

    [HarmonyPostfix]
    [HarmonyPatch(typeof(WeatherSimulationRegion), "RandomWeatherEvent")]
    public static void WeatherEvent_Postfix(WeatherSimulationRegion __instance, float? __state)
    {
        if (__state.HasValue) __instance.weatherData.climateCond.WorldgenRainfall = __state.Value;
    }

    public static bool UpdateMonthlyTemperature(ref ClimateCondition climate, BlockPos pos, EnumGetClimateMode mode, double totalDays, ModTemperature __instance, ICoreAPI ___api)
    {
        var api = ___api;
        EarthClimate earth = api.Side == EnumAppSide.Server ? EarthMapCreator.ClimateData : EarthMapCreator.ClientClimateData;
        if (earth == null || !earth.Contains(pos.X, pos.Z)) return true;
        if (mode == EnumGetClimateMode.WorldGenValues) return false;

        double yearRel = totalDays / api.World.Calendar.DaysPerYear;
        float? overrideSeason = api.World.Calendar.SeasonOverride;
        float average = earth.MonthlyTemperature(pos.X, pos.Z, overrideSeason ?? yearRel);
        // CHELSA describes air at the smoothed terrain; cool higher blocks
        // (summits, and the world-top points of vanilla's snow simulation)
        // and warm lower ones. Heights compare in source (0-255) Y.
        int mapSizeY = api.World.BlockAccessor.MapSizeY;
        average += earth.AltitudeOffset(pos.X, pos.Z, pos.Y * 255f / (mapSizeY - 1));
        double hour = (totalDays % 1) * api.World.Calendar.HoursPerDay;
        double diurnalAmplitude = 18 - climate.Rainfall * 13;
        double dayPhase = GameMath.SmoothStep(Math.Abs(GameMath.CyclicValueDistance(4, hour, 24) / 12f));
        double diurnal = (dayPhase - 0.5) * diurnalAmplitude;
        double yearlyNoise = __instance.YearlyTemperatureNoise.Noise(totalDays, 0) * 3;
        double dailyNoise = __instance.DailyTemperatureNoise.Noise(totalDays, 0);
        climate.Temperature = average + (float)(diurnal + yearlyNoise + dailyNoise);
        return false;
    }

}