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
        PotentialSurface.Initialize(api);
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
    // PNV surfaces are placed explicitly; vanilla's block-layer rainfall,
    // soil-thickness and fertility selection are not used.

    // Vanilla's snow pass tests annual climate against -10 °C. Generate
    // initial snow only where the current in-game climate is below freezing;
    // the weather simulation handles later accumulation and melting.
    [HarmonyPrefix]
    [HarmonyPatch(typeof(GenSnowLayer), "OnChunkColumnGen")]
    public static bool SnowLayerAtCurrentTemperature(IChunkColumnGenerateRequest request)
    {
        var api = EarthMapPatches._api;
        var layers = EarthMapCreator.Layers;
        int x = request.ChunkX * api.WorldManager.ChunkSize;
        int z = request.ChunkZ * api.WorldManager.ChunkSize;
        if (!layers.Contains(x, z)) return true;
        PotentialSurface.PlaceSeasonalSnow(api, request);
        return false;
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

    // Vanilla interpolates the forest map and gives trees a nonzero minimum chance
    // even at zero density. Shrubs set skipForestFloor; guard only actual trees.
    [HarmonyPrefix]
    [HarmonyPatch(typeof(TreeGenInstance), nameof(TreeGenInstance.GrowTree))]
    public static bool TreeGenInstance_GrowTree_Prefix(TreeGenInstance __instance, BlockPos pos)
    {
        if (__instance.skipForestFloor) return true;
        EarthClimate climate = EarthMapCreator.ClimateData;
        return climate == null || !climate.Contains(pos.X, pos.Z) ||
            climate.WarmestMonthTemperature(pos.X, pos.Z) >= PotentialVegetation.TreeGrowingSeasonTemperature;
    }

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
        // Bit 7 of the season-map byte tells our shader that this byte already
        // describes air at local terrain, not vanilla sea-level air.
        __result.Value = (__result.Value & ~(0xFF << 16)) | (temp << 16);
        if ((__result.Value & 0x3F) != 0 && (__result.Value & 0xC0) == 0)
            __result.Value |= 0x80;
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


    
    [HarmonyPrefix]
    [HarmonyPatch(typeof(GenBlockLayers), "OnChunkColumnGeneration", new Type[] { typeof(IChunkColumnGenerateRequest) })]
    public static bool GenBlockLayers_OnChunkColumnGen_Prefix(IChunkColumnGenerateRequest request)
    {
        var api = EarthMapPatches._api;
        var layers = EarthMapCreator.Layers;
        var climate = EarthMapCreator.ClimateData;
        var chunks = request.Chunks;
        int size = api.WorldManager.ChunkSize;
        int originX = request.ChunkX * size;
        int originZ = request.ChunkZ * size;
        ushort[] rainHeight = chunks[0].MapChunk.RainHeightMap;
        ushort[] groundHeight = chunks[0].MapChunk.WorldGenTerrainHeightMap;
        for (int z = 0; z < size; z++)
        {
            for (int x = 0; x < size; x++)
            {
                int worldX = originX + x, worldZ = originZ + z;
                if (!layers.Contains(worldX, worldZ)) continue;
                bool ocean = layers.Get(MapPlane.LandMask, worldX, worldZ) == 0;
                bool lake = layers.Get(MapPlane.LakeMask, worldX, worldZ) != 0;
                bool river = layers.Get(MapPlane.River, worldX, worldZ) != 0;
                if (!ocean && !lake && !river && layers.Get(MapPlane.Vegetation, worldX, worldZ) == 0) continue;
                int index = z * size + x;
                int ground = groundHeight[index];
                int surface = rainHeight[index];
                if (ground <= 0 || ground >= api.WorldManager.MapSizeY) continue;
                var data = chunks[ground / size].Data;
                int blockIndex = ((ground % size) * size + z) * size + x;
                int rockId = data.GetBlockIdUnsafe(blockIndex);
                if (api.World.Blocks[rockId].BlockMaterial != EnumBlockMaterial.Stone) continue;

                float temp = climate.AnnualTemperature(worldX, worldZ);
                float warmest = climate.WarmestMonthTemperature(worldX, worldZ);
                float wetness = climate.VegetationWetness(worldX, worldZ);
                int biome = layers.Get(MapPlane.Vegetation, worldX, worldZ);
                var profile = PotentialVegetation.Get(biome, temp, warmest, wetness);
                PotentialSurface.Place(api, chunks, x, z, worldX, worldZ, surface, ground,
                    rockId, profile, biome, temp, wetness, profile.Forest, ocean, lake, river);
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
        if (sapi == null) return true;

        long worldSeed = sapi.WorldManager.Seed;
        bool forest = seed == worldSeed + 2 && scale == TerraGenConfig.forestMapScale;
        bool shrub = seed == worldSeed + 109 && scale == TerraGenConfig.shrubMapScale;
        if (!forest && !shrub)
        {
            return true; // The seed + 223 biome generator retains its vanilla data.
        }

        sapi.Logger.Notification($"[EarthMapCreator] Overwriting {(forest ? "forest" : "shrub")} map with potential natural vegetation.");
        __result = new MapLayerFromImage(
            seed,
            EarthMapCreator.Layers,
            MapPlane.Vegetation,
            sapi,
            scale,
            forest ? ForestMapProcessor.ForestPostProcess : ForestMapProcessor.ShrubPostProcess);
        return false;
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

    // Vanilla records snow at sea level and world top, then interpolates the
    // *accumulation rates* at the ground. CHELSA's terrain-relative lapse can
    // put sea level above freezing and the ground below it; interpolating the
    // melt and snowfall branches then cancels real snowfall. Sample the same
    // hourly climate at the actual ground for both vertical snapshot corners.
    [HarmonyPrefix]
    [HarmonyPatch(typeof(WeatherSimulationRegion), nameof(WeatherSimulationRegion.UpdateSnowAccumulation))]
    public static void SnowAccumulationStart(WeatherSimulationRegion __instance, out double __state) =>
        __state = __instance.LastUpdateTotalHours;

    [HarmonyPostfix]
    [HarmonyPatch(typeof(WeatherSimulationRegion), nameof(WeatherSimulationRegion.UpdateSnowAccumulation))]
    public static void SnowAccumulationAtTerrain(WeatherSimulationRegion __instance, int count,
        double __state, WeatherSystemBase ___ws)
    {
        EarthClimate earth = EarthMapCreator.ClimateData;
        if (earth == null || count <= 0 || __instance.LastUpdateTotalHours != __state + count) return;

        var world = ___ws.api.World;
        int regionSize = world.BlockAccessor.RegionSize;
        float hoursPerDay = world.Calendar.HoursPerDay;
        int top = world.BlockAccessor.MapSizeY - 1;
        int resolution = WeatherSimulationRegion.snowAccumResolution;
        var snapshots = new SnowAccumSnapshot[count];
        lock (WeatherSimulationRegion.snowAccumSnapshotLock)
        {
            bool found = false;
            for (int i = 0; i < __instance.SnowAccumSnapshots.Length; i++)
            {
                SnowAccumSnapshot snapshot = __instance.SnowAccumSnapshots[i];
                if (snapshot == null) continue;
                int hour = (int)(snapshot.TotalHours - __state);
                if ((uint)hour < (uint)snapshots.Length && snapshot.TotalHours == __state + hour)
                {
                    snapshots[hour] = snapshot;
                    found = true;
                }
            }
            if (!found) return;

            var pos = new BlockPos(0);
            for (int x = 0; x < resolution; x++)
            for (int z = 0; z < resolution; z++)
            {
                pos.X = __instance.regionX * regionSize + x * (regionSize - 1);
                pos.Z = __instance.regionZ * regionSize + z * (regionSize - 1);
                if (!earth.Contains(pos.X, pos.Z)) continue;
                float sourceY = earth.ReferenceTerrainY(pos.X, pos.Z);
                if (!float.IsFinite(sourceY)) continue;
                pos.Y = Math.Clamp(RegionStore.WorldY((int)MathF.Round(sourceY), top + 1), 0, top);

                ClimateCondition climate = null;
                for (int hour = 0; hour < snapshots.Length; hour++)
                {
                    double totalHours = __state + hour;
                    double day = (totalHours + 0.5) / hoursPerDay;
                    if (climate == null)
                        climate = world.BlockAccessor.GetClimateAt(pos, EnumGetClimateMode.ForSuppliedDate_TemperatureRainfallOnly, day);
                    else
                        world.BlockAccessor.GetClimateAt(pos, climate, EnumGetClimateMode.ForSuppliedDate_TemperatureRainfallOnly, day);
                    if (climate == null) break;
                    float rate = climate.Temperature > 1.5f || (climate.Rainfall < 0.05f && climate.Temperature > 0f)
                        ? -climate.Temperature / 15f : climate.Rainfall / 3f;
                    SnowAccumSnapshot snapshot = snapshots[hour];
                    if (snapshot == null) continue;
                    // Replace, rather than add to, both sea/top rates. The snow
                    // layer scanner still uses vanilla's height interpolation.
                    snapshot.SnowAccumulationByRegionCorner.AddValue(x, 0, z,
                        rate - snapshot.SnowAccumulationByRegionCorner.GetValue(x, 0, z));
                    snapshot.SnowAccumulationByRegionCorner.AddValue(x, 1, z,
                        rate - snapshot.SnowAccumulationByRegionCorner.GetValue(x, 1, z));
                }
            }
        }
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