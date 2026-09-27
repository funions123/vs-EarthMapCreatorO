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
        
        var layers = EarthMapCreator.Layers;
        if (!layers.Contains(pos.X, pos.Z))
            return TextCommandResult.Error("Position is outside the Earth map");
        int regionX = pos.X / _api.WorldManager.RegionSize;
        int regionZ = pos.Z / _api.WorldManager.RegionSize;
        int relativeX = pos.X % _api.WorldManager.RegionSize;
        int relativeZ = pos.Z % _api.WorldManager.RegionSize;
        MapRegion region = layers.GetRegion(regionX, regionZ);
        int treeHere = region.Get(MapPlane.Tree, relativeX, relativeZ);
        int terrainHere = region.Get(MapPlane.Height, relativeX, relativeZ);
        int bathyHere = region.Get(MapPlane.Bathymetry, relativeX, relativeZ);
        int lakeDepthHere = region.Get(MapPlane.LakeDepth, relativeX, relativeZ);
        int lakeBedHere = Math.Max(1, terrainHere - lakeDepthHere);
        int landMaskHere = region.Get(MapPlane.LandMask, relativeX, relativeZ);
        int lakeMaskHere = region.Get(MapPlane.LakeMask, relativeX, relativeZ);
        int riverMaskHere = region.Get(MapPlane.River, relativeX, relativeZ);
        int riverSurfaceHere = region.Get(MapPlane.RiverSurface, relativeX, relativeZ);
        int riverDepthHere = region.Get(MapPlane.RiverDepth, relativeX, relativeZ);
        ClimateZone zone = (ClimateZone)region.Get(MapPlane.Climate, relativeX, relativeZ);
        
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
