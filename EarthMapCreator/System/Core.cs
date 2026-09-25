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
    public static MapLayers Layers; 

    public override void Start(ICoreAPI api)
    {
        LoadConfig(api);
        LoadMapLayers(api);

        TerraGenConfig.seaLevel = 92; //certain subroutines chimp out at certain times for below sea level stuff
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
        Mod.Logger.Notification("Loading Earth map layers from {0}", folder);
        Layers = new MapLayers(folder);
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
