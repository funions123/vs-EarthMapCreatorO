using System;
using Vintagestory.API.Common;
using Vintagestory.API.Datastructures;
using Vintagestory.API.MathTools;
using Vintagestory.API.Server;

namespace EarthMapCreator;

public class EarthWorldGenerator : ModSystem
{
    private ICoreServerAPI _api;

    public override void StartServerSide(ICoreServerAPI api)
    {
        this._api = api;
        InitCommands();
    }

    private void InitCommands()
    {
        _api.ChatCommands.GetOrCreate("earthmap")
            .WithDescription("Earth map commands")
            .RequiresPrivilege(Privilege.controlserver)
                .BeginSubCommand("pos")
                    .RequiresPlayer()
                    .WithDescription("Info about current position")
                    .HandleWith(Cmd_OnPos)
                .EndSubCommand()
                .BeginSubCommand("climate")
                    .RequiresPlayer()
                    .WithDescription("Sample climate at your position")
                    .HandleWith(Cmd_OnClimate)
                .EndSubCommand();
    }

    private TextCommandResult Cmd_OnPos(TextCommandCallingArgs args)
    {
        var player = args.Caller.Player;
        BlockPos pos = player.Entity.Pos.AsBlockPos;
        
        var layers = EarthMapCreator.Layers;
        if (!layers.Contains(pos.X, pos.Z))
            return TextCommandResult.Error("Position is outside the Earth map");
        int regionX = pos.X / _api.WorldManager.RegionSize;
        int regionZ = pos.Z / _api.WorldManager.RegionSize;
        int relativeX = pos.X % _api.WorldManager.RegionSize;
        int relativeZ = pos.Z % _api.WorldManager.RegionSize;
        MapRegion region = layers.GetRegion(regionX, regionZ);
        int vegetationHere = region.Get(MapPlane.Vegetation, relativeX, relativeZ);
        int worldHeight = _api.WorldManager.MapSizeY;
        int terrainHere = RegionStore.WorldY(region.Get(MapPlane.Height, relativeX, relativeZ), worldHeight);
        int bathyHere = RegionStore.WorldY(Math.Max(1, region.Get(MapPlane.Bathymetry, relativeX, relativeZ) - 1), worldHeight);
        int lakeDepthHere = region.Get(MapPlane.LakeDepth, relativeX, relativeZ);
        int lakeBedHere = RegionStore.WorldY(Math.Max(1, region.Get(MapPlane.Height, relativeX, relativeZ) - lakeDepthHere), worldHeight);
        int landMaskHere = region.Get(MapPlane.LandMask, relativeX, relativeZ);
        int lakeMaskHere = region.Get(MapPlane.LakeMask, relativeX, relativeZ);
        int riverMaskHere = region.Get(MapPlane.River, relativeX, relativeZ);
        int riverSurfaceHere = RegionStore.WorldY(region.Get(MapPlane.RiverSurface, relativeX, relativeZ), worldHeight);
        int riverDepthHere = region.Get(MapPlane.RiverDepth, relativeX, relativeZ);
        EarthClimate climate = EarthMapCreator.ClimateData;
        float annualTemperature = climate.AnnualTemperature(pos.X, pos.Z);
        float warmestTemperature = climate.WarmestMonthTemperature(pos.X, pos.Z);
        float vegetationWetness = climate.VegetationWetness(pos.X, pos.Z);
        VegetationProfile vegetation = PotentialVegetation.Get(
            vegetationHere, annualTemperature, warmestTemperature, vegetationWetness);
        String msg = $"At {pos.X}, {pos.Z}, (region {regionX}, {regionZ})\n" +
                     $"Climate - annual {annualTemperature:F1}°C, " +
                     $"P {climate.AnnualPrecipitation(pos.X, pos.Z):F0} mm/year, " +
                     $"vegetation wetness {vegetationWetness:F2}, " +
                     $"monthly {climate.MonthlyTemperature(pos.X, pos.Z, _api.World.Calendar.YearRel):F1}°C\n" +
                     $"EMREGION v4 PNV class: {vegetationHere}; forest: {vegetation.Forest}; shrub: {vegetation.Shrub}; " +
                     $"grass: {vegetation.Grass:F2}; fertility: {vegetation.Fertility:F2}; soil depth: {vegetation.SoilDepth}; " +
                     $"bare: {vegetation.Bare}; snow: {vegetation.Snow}\n";

        msg += $"Bathy (bed Y: {bathyHere})\n";
        msg += $"Land (Height: {terrainHere})\n";
        msg += $"Lake depth: {terrainHere - lakeBedHere} blocks (bed Y: {lakeBedHere})\n";
        msg += $"Land Mask: {landMaskHere}\n";
        msg += $"Lake Mask: {lakeMaskHere}\n";
        msg += $"River: {riverMaskHere > 0} (surface Y: {riverSurfaceHere}, depth: {riverSurfaceHere - RegionStore.WorldY(Math.Max(1, region.Get(MapPlane.RiverSurface, relativeX, relativeZ) - riverDepthHere), worldHeight)} blocks)\n";
        
        return TextCommandResult.Success(msg);
    }

    private TextCommandResult Cmd_OnClimate(TextCommandCallingArgs args)
    {
        BlockPos pos = args.Caller.Player.Entity.Pos.AsBlockPos;
        EarthClimate climate = EarthMapCreator.ClimateData;
        if (climate == null || !climate.Contains(pos.X, pos.Z))
            return TextCommandResult.Error("Position is outside the Earth climate map");

        return TextCommandResult.Success(FormatClimate(climate, pos.X, pos.Z));
    }

    internal static string FormatClimate(EarthClimate climate, int x, int z)
    {
        float p = climate.AnnualPrecipitation(x, z);
        float pet = climate.AnnualPotentialEvapotranspiration(x, z);
        float effectiveRain = climate.VegetationWetness(x, z);
        var message = new System.Text.StringBuilder();
        message.Append($"Climate at {x}, {z} (loaded EMCL data)\n")
            .Append($"Annual mean temperature: {climate.AnnualTemperature(x, z):F1} °C; warmest month: {climate.WarmestMonthTemperature(x, z):F1} °C; snowpack: {climate.WarmestMonthTemperature(x, z) < EarthMapCreator.config.SnowpackWarmestMonthTemperature}\n")
            .Append($"P: {p:F0} mm/year; PET: {pet:F0} mm/year\n")
            .Append($"P/(P+max(PET,1)): {p / (p + Math.Max(pet, 1f)):F3}; P/200 cap: {Math.Min(1f, p / 200f):F3}\n")
            .Append($"Effective rainfall (vegetation wetness): {effectiveRain:F3}; soil-layer wetness: {(climate.WarmestMonthTemperature(x, z) < EarthClimate.GrassGrowingSeasonTemperature ? 0f : effectiveRain):F3}; weather rain factor P/(P+800): {p / (p + 800f):F3}\n")
            .Append("Monthly mean temperature (°C):\n");
        for (int month = 0; month < 12; month++)
        {
            if (month > 0) message.Append(month == 6 ? '\n' : ' ');
            message.Append($"{month + 1:00}: {climate.MonthlyTemperature(x, z, (month + 0.5) / 12.0):F1}");
        }
        return message.ToString();
    }

}
