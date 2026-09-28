﻿using System;
using System.IO;
using System.Reflection;
using HarmonyLib;
using Vintagestory.API.Common;
using Vintagestory.API.Server;
using Vintagestory.ServerMods;

namespace EarthMapCreator;

public class EarthMapCreator : ModSystem
{
    public static Config config;
    public static RegionStore Layers;
    public static EarthClimate ClimateData;
    public static EarthClimate ClientClimateData;
    private bool loadedServerMaps;

    public override void Start(ICoreAPI api)
    {
        if (api.Side != EnumAppSide.Server) return;
        LoadConfig(api);
        LoadMapLayers(api);
        loadedServerMaps = true;
    }

    public override void AssetsFinalize(ICoreAPI api)
    {
        if (api.Side != EnumAppSide.Server) return;
        int seaLevel = RegionStore.WorldY(Layers.SeaLevel, ((ICoreServerAPI)api).WorldManager.MapSizeY);
        ((ICoreServerAPI)api).WorldManager.SetSeaLevel(seaLevel);
        TerraGenConfig.seaLevel = seaLevel;
        Vintagestory.API.Common.Climate.Sealevel = seaLevel;
    }

    private void LoadConfig(ICoreAPI api) 
    {
        try 
        {
            config = api.LoadModConfig<Config>("EarthMapCreator.json");
            if (config == null)
            {
                config = new Config();
            }
            api.StoreModConfig<Config>(config, "EarthMapCreator.json");
        }
        catch (Exception e)
        {
            Mod.Logger.Error("Failed to load configuration - Using defaults");
            Mod.Logger.Error(e);
            config = new Config();
        }
    }

    private void LoadMapLayers(ICoreAPI api)
    {
        string folder = ResolveMapDirectory(api);
        Mod.Logger.Notification("Loading Earth map regions from {0}", folder);
        Layers = new RegionStore(Path.Combine(folder, "earthmap.regions"));
        ClimateData = new EarthClimate(Path.Combine(folder, "earthclimate.bin"), Layers.Width, Layers.Height);
    }

    public override void Dispose()
    {
        if (!loadedServerMaps) return;
        ClimateData = null;
        Layers.Dispose();
        Layers = null;
    }

    internal static string ResolveMapDirectory(ICoreAPI api)
    {
        string assemblyDirectory = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location)!;
        string debugPathFile = Path.Combine(assemblyDirectory, "earthmap-debug-path.txt");

        if (!File.Exists(debugPathFile))
        {
            return api.GetOrCreateDataPath("EarthMap");
        }

        string folder = File.ReadAllText(debugPathFile).Trim();
        if (!Directory.Exists(folder))
        {
            throw new DirectoryNotFoundException($"Staged Earth map directory does not exist: {folder}");
        }

        return folder;
    }
}
