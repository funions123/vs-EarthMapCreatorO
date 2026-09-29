using System;
using System.Collections.Generic;
using Vintagestory.API.Common;
using Vintagestory.API.MathTools;
using Vintagestory.API.Server;
using Vintagestory.ServerMods;

namespace EarthMapCreator;

/// <summary>Places potential-vegetation ground cover without vanilla's climate-derived soil stack.</summary>
internal static class PotentialSurface
{
    private static readonly string[] Coverage = { "none", "verysparse", "sparse", "normal" };
    private static readonly string[] Fertility = { "verylow", "low", "medium", "high" };
    private static readonly int[,] Soil = new int[4, 4];
    private static readonly int[] ForestFloor = new int[8];
    private static readonly Dictionary<int, int> Gravel = new();
    private static readonly Dictionary<int, int> Sand = new();
    private static int snowBlock;
    private static int snowLayer;

    internal static void Initialize(ICoreServerAPI api)
    {
        var world = api.World;
        for (int f = 0; f < Fertility.Length; f++)
            for (int c = 0; c < Coverage.Length; c++)
                Soil[f, c] = world.GetBlock(new AssetLocation("game", $"soil-{Fertility[f]}-{Coverage[c]}"))?.Id
                    ?? throw new InvalidOperationException($"Missing potential surface soil {Fertility[f]}-{Coverage[c]}");
        for (int stage = 0; stage < ForestFloor.Length; stage++)
            ForestFloor[stage] = world.GetBlock(new AssetLocation("game", $"forestfloor-{stage}"))?.Id
                ?? throw new InvalidOperationException($"Missing forest floor stage {stage}");
        snowBlock = world.GetBlock(new AssetLocation("game", "snowblock"))?.Id
            ?? throw new InvalidOperationException("Missing snowblock");
        snowLayer = world.GetBlock(new AssetLocation("game", "snowlayer-1"))?.Id
            ?? throw new InvalidOperationException("Missing snowlayer-1");

        Gravel.Clear();
        Sand.Clear();
        var strata = BlockLayerConfig.GetInstance(api).RockStrata;
        foreach (var stratum in strata.Variants)
        {
            if (stratum.IsDeposit) continue;
            Block rock = world.GetBlock(stratum.BlockCode);
            if (rock == null) continue;
            string rockType = stratum.BlockCode.Path.Split('-')[1];
            Block gravel = world.GetBlock(new AssetLocation("game", $"gravel-{rockType}"));
            Block sand = world.GetBlock(new AssetLocation("game", $"sand-{rockType}"));
            if (gravel != null) Gravel[rock.Id] = gravel.Id;
            if (sand != null) Sand[rock.Id] = sand.Id;
        }
    }

    private static float Random01(int x, int z, int salt) =>
        (uint)GameMath.MurmurHash3(x, salt, z) / (float)uint.MaxValue;

    private static int MaterialFor(int rockId, bool sandy)
    {
        var map = sandy ? Sand : Gravel;
        return map.TryGetValue(rockId, out int material) ? material : rockId;
    }

    internal static (int fertility, int cover, int forestStage, float grass) GroundCover(
        VegetationProfile profile, int biome, int forest, float temperature, bool nearRiver,
        float coverRoll)
    {
        int fertility = profile.Fertility >= 0.7f ? 3 : profile.Fertility >= 0.3f ? 2 : profile.Fertility >= 0.15f ? 1 : 0;
        if (nearRiver && fertility < 2) fertility = 2;
        float grass = profile.Grass;
        if (nearRiver && temperature >= -10f)
            grass = Math.Max(grass, Math.Min(0.2f, 0.08f + profile.Grass * 0.6f));
        int cover = grass >= 0.65f ? 3 : grass >= 0.3f ? 2 : grass >= 0.1f ? 1 : 0;
        if (temperature < -10f) cover = 0;
        else if (fertility < 2 && coverRoll > grass) cover = 0;
        if (nearRiver && temperature >= -10f && cover == 0 && grass > 0f)
            cover = 1;
        if (fertility >= 2 && temperature >= -10f && cover == 0)
            cover = 1;
        int forestStage = forest >= 96 && (biome is >= 1 and <= 4 or >= 7 and <= 9 or >= 13 and <= 15 or 17)
            && temperature >= -10f ? Math.Clamp((forest - 64) / 32, 1, 7) : -1;
        return (fertility, cover, forestStage, grass);
    }

    internal static void Place(ICoreServerAPI api, IServerChunk[] chunks, int localX, int localZ,
        int x, int z, int surfaceY, int groundY, int rockId, VegetationProfile profile,
        int biome, float temperature, float wetness, int forest, bool ocean, bool lake, bool river)
    {
        int size = api.WorldManager.ChunkSize;
        bool submerged = surfaceY > groundY || ocean || lake || river;
        var config = BlockLayerConfig.GetInstance(api);
        int top;
        int under = 0;
        int depth;
        float effectiveGrass = 0;
        bool hasSoil = false;
        if (submerged)
        {
            top = 0;
            var bed = ocean ? config.OceanBedLayer : config.LakeBedLayer;
            foreach (var candidate in bed.BlockCodeByMin)
            {
                if (!candidate.Suitable(temperature, wetness, groundY / (float)api.WorldManager.MapSizeY,
                    Random01(x, z, 9))) continue;
                top = candidate.GetBlockForMotherRock(rockId);
                if (top != 0) break;
            }
            depth = top == 0 ? 0 : 1;
        }
        else if (profile.Snow)
        {
            top = snowBlock;
            depth = 1;
        }
        else
        {
            bool nearRiver = IsNearRiver(EarthMapCreator.Layers, x, z);
            float slope = Slope(EarthMapCreator.Layers, x, z);
            float bareChance = profile.Bare ? 0.45f : 0.03f;
            bareChance = Math.Clamp(bareChance + slope * 0.14f - (nearRiver ? 0.22f : 0f), 0f, 0.95f);
            bool bare = Random01(x, z, 1) < bareChance || profile.SoilDepth == 0;
            if (bare)
            {
                bool sandy = biome == 27 && wetness < 0.2f && slope < 1f && Random01(x, z, 2) < 0.5f;
                top = MaterialFor(rockId, sandy);
                depth = 1;
            }
            else
            {
                var (fertility, cover, forestStage, grass) = GroundCover(
                    profile, biome, forest, temperature, nearRiver, Random01(x, z, 3));
                top = forestStage >= 0 ? ForestFloor[forestStage] : Soil[fertility, cover];
                under = Soil[fertility, 0];
                hasSoil = true;
                depth = Math.Clamp(profile.SoilDepth - (int)slope + (nearRiver ? 1 : 0), 1, 4);
                if (cover > 0 && forestStage < 0) effectiveGrass = grass;
            }
        }
        if (depth == 0) return;
        chunks[0].MapChunk.TopRockIdMap[localZ * size + localX] = rockId;
        for (int i = 0; i < depth && groundY - i > 0; i++)
        {
            int y = groundY - i;
            var blocks = chunks[y / size].Data;
            int index = ((y % size) * size + localZ) * size + localX;
            if (blocks.GetBlockIdUnsafe(index) != rockId) break;
            blocks.SetBlockUnsafe(index, i == 0 ? top : under);
        }
        if (!hasSoil || effectiveGrass <= 0.15f || groundY + 1 >= api.WorldManager.MapSizeY) return;
        var above = chunks[(groundY + 1) / size].Data;
        int aboveIndex = (((groundY + 1) % size) * size + localZ) * size + localX;
        if (above.GetBlockIdUnsafe(aboveIndex) != 0 || above.GetFluid(aboveIndex) != 0) return;
        if (Random01(x, z, 5) >= effectiveGrass * (1f - forest / 320f) * 0.35f) return;
        // Use vanilla's resolved tallgrass variants, but not its rainfall-derived density.
        foreach (var variant in config.Tallgrass.BlockCodeByMin)
        {
            if (temperature < variant.MinTemp || effectiveGrass < variant.MinRain || forest / 255f > variant.MaxForest) continue;
            above.SetBlockUnsafe(aboveIndex, variant.BlockId);
            break;
        }
    }

    internal static void PlaceSeasonalSnow(ICoreServerAPI api, IChunkColumnGenerateRequest request)
    {
        var chunks = request.Chunks;
        int size = api.WorldManager.ChunkSize;
        int maxY = api.WorldManager.MapSizeY - 1;
        int x0 = request.ChunkX * size, z0 = request.ChunkZ * size;
        ushort[] rainHeight = chunks[0].MapChunk.RainHeightMap;
        var pos = new BlockPos(0);
        for (int z = 0; z < size; z++)
        for (int x = 0; x < size; x++)
        {
            int worldX = x0 + x, worldZ = z0 + z;
            if (!EarthMapCreator.Layers.Contains(worldX, worldZ)) continue;
            int column = z * size + x;
            int groundY = rainHeight[column];
            if (groundY <= 0 || groundY >= maxY) continue;
            pos.X = worldX;
            pos.Z = worldZ;
            pos.Y = groundY;
            var climate = api.World.BlockAccessor.GetClimateAt(pos, EnumGetClimateMode.NowValues);
            if (climate == null || !float.IsFinite(climate.Temperature) || climate.Temperature > 0f) continue;
            int surfaceY = groundY;
            while (surfaceY < maxY)
            {
                int aboveY = surfaceY + 1;
                int aboveIndex = ((aboveY % size) * size + z) * size + x;
                if (chunks[aboveY / size].Data.GetBlockIdUnsafe(aboveIndex) == 0) break;
                surfaceY = aboveY;
            }
            if (surfaceY >= maxY) continue;
            int baseIndex = ((surfaceY % size) * size + z) * size + x;
            int baseId = chunks[surfaceY / size].Data.GetFluid(baseIndex);
            if (baseId == 0) baseId = chunks[surfaceY / size].Data.GetBlockIdUnsafe(baseIndex);
            if (!api.World.Blocks[baseId].SideSolid[BlockFacing.UP.Index]) continue;
            int snowY = surfaceY + 1;
            int snowIndex = ((snowY % size) * size + z) * size + x;
            if (chunks[snowY / size].Data.GetFluid(snowIndex) != 0) continue;
            chunks[snowY / size].Data.SetBlockUnsafe(snowIndex, snowLayer);
            // Vanilla's scanner removes a one-layer block unless accumulated
            // snow exceeds 1.1 plus up to 0.5 of per-column variation.
            // Bootstrap its accumulation map with the layer we generated.
            chunks[0].MapChunk.SnowAccum[column] = 1.6f;
            rainHeight[column]++;
        }
    }

    private static float Slope(RegionStore layers, int x, int z)
    {
        int center = layers.Get(MapPlane.Height, x, z);
        int max = 0;
        if (x > 0) max = Math.Max(max, Math.Abs(center - layers.Get(MapPlane.Height, x - 1, z)));
        if (z > 0) max = Math.Max(max, Math.Abs(center - layers.Get(MapPlane.Height, x, z - 1)));
        if (x + 1 < layers.Width) max = Math.Max(max, Math.Abs(center - layers.Get(MapPlane.Height, x + 1, z)));
        if (z + 1 < layers.Height) max = Math.Max(max, Math.Abs(center - layers.Get(MapPlane.Height, x, z + 1)));
        return max;
    }

    private static bool IsNearRiver(RegionStore layers, int x, int z)
    {
        const int radius = 4;
        for (int dz = -radius; dz <= radius; dz++)
            for (int dx = -radius; dx <= radius; dx++)
                if (dx * dx + dz * dz <= radius * radius && layers.Contains(x + dx, z + dz) &&
                    layers.Get(MapPlane.River, x + dx, z + dz) != 0) return true;
        return false;
    }
}
