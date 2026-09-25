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
                .EndSubCommand();
    }

    private TextCommandResult Cmd_OnPos(TextCommandCallingArgs args)
    {
        var player = args.Caller.Player;
        BlockPos pos = player.Entity.Pos.AsBlockPos;
        
        int regionX = pos.X / _api.WorldManager.RegionSize;
        int regionZ = pos.Z / _api.WorldManager.RegionSize;

        int relativeX = pos.X - regionX * _api.WorldManager.RegionSize;
        int relativeZ = pos.Z - regionZ * _api.WorldManager.RegionSize;
        
        IntDataMap2D tree = EarthMapCreator.Layers.TreeMap.IntValues[regionX][regionZ];
        int treeHere = tree.GetInt(relativeX, relativeZ); 
        
        IntDataMap2D terrain = EarthMapCreator.Layers.HeightMap.IntValues[regionX][regionZ];
        int terrainHere = terrain.GetInt(relativeX, relativeZ);
        
        IntDataMap2D bathyMap = EarthMapCreator.Layers.OceanBathyMap.IntValues[regionX][regionZ];
        int bathyHere = bathyMap.GetInt(relativeX, relativeZ);
        
        IntDataMap2D lakeDepthMap = EarthMapCreator.Layers.LakeDepthMap.IntValues[regionX][regionZ];
        int lakeDepthHere = lakeDepthMap.GetInt(relativeX, relativeZ);
        int lakeBedHere = Math.Max(1, terrainHere - lakeDepthHere);
        
        IntDataMap2D landMaskMap = EarthMapCreator.Layers.LandMaskMap.IntValues[regionX][regionZ];
        int landMaskHere = landMaskMap.GetInt(relativeX, relativeZ);
        
        IntDataMap2D lakeMaskMap = EarthMapCreator.Layers.LakeMaskMap.IntValues[regionX][regionZ];
        int lakeMaskHere = lakeMaskMap.GetInt(relativeX, relativeZ);
        int riverMaskHere = EarthMapCreator.Layers.RiverMap.IntValues[regionX][regionZ].GetInt(relativeX, relativeZ);
        int riverSurfaceHere = EarthMapCreator.Layers.RiverSurfaceMap.IntValues[regionX][regionZ].GetInt(relativeX, relativeZ);
        int riverDepthHere = EarthMapCreator.Layers.RiverDepthMap.IntValues[regionX][regionZ].GetInt(relativeX, relativeZ);
        
        int zoneId = EarthMapCreator.Layers.ClimateMap.IntValues[regionX][regionZ].GetInt(relativeX, relativeZ);
        ClimateZone zone = (ClimateZone)zoneId;
        (float temp, float rainRel) climate = ClimateMatcher.GetClimateValues(zone);
        
        String msg = $"At {pos.X}, {pos.Z}, (region {regionX}, {regionZ})\n" +
                     $"Climate - {zone}\n" +
                     $"Tree: {treeHere}\n";

        msg += $"Bathy (Height: {bathyHere})\n";
        msg += $"Land (Height: {terrainHere})\n";
        msg += $"Lake depth: {lakeDepthHere} (bed height: {lakeBedHere})\n";
        msg += $"Land Mask: {landMaskHere}\n";
        msg += $"Lake Mask: {lakeMaskHere}\n";
        msg += $"River: {riverMaskHere > 0} (surface: {riverSurfaceHere}, depth: {riverDepthHere})\n";
        
        return TextCommandResult.Success(msg);
    }

}
