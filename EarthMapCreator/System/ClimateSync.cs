using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using ProtoBuf;
using Vintagestory.API.Client;
using Vintagestory.API.Common;
using Vintagestory.API.Config;
using Vintagestory.API.MathTools;
using Vintagestory.API.Server;

namespace EarthMapCreator;

[ProtoContract]
public sealed class ClimateTileRequest
{
    [ProtoMember(1)] public int RegionX;
    [ProtoMember(2)] public int RegionZ;
    [ProtoMember(3)] public int WitnessChunkX;
    [ProtoMember(4)] public int WitnessChunkY;
    [ProtoMember(5)] public int WitnessChunkZ;
    [ProtoMember(6)] public bool Release;
}

[ProtoContract]
public sealed class ClimateTileChunk
{
    [ProtoMember(1)] public int RegionX;
    [ProtoMember(2)] public int RegionZ;
    [ProtoMember(3)] public int Index;
    [ProtoMember(4)] public int Count;
    [ProtoMember(5)] public int CompressedLength;
    [ProtoMember(6)] public int RawLength;
    [ProtoMember(7)] public int MapWidth;
    [ProtoMember(8)] public int MapHeight;
    [ProtoMember(9)] public int Spacing;
    [ProtoMember(10)] public float LapsePerBlock;
    [ProtoMember(11)] public byte[] Data = Array.Empty<byte>();
}

/// <summary>Synchronizes only the 512-block climate regions surrounding map
/// regions currently loaded by the client. Each tile includes its positive-X
/// and positive-Z sample halo, so interpolation is identical at tile seams.</summary>
public sealed class ClimateSync : ModSystem
{
    private const int PacketBytes = 32768;
    private const int MaxCompressedTileBytes = EarthClimate.TileByteLength + 1024;
    private const int MaxPendingTransfers = 16;
    private const int MaxRequestsPerTick = 2;
    private const int MaxServedTilesPerPlayer = EarthClimate.MaxClientCachedTiles;

    private IServerNetworkChannel serverChannel;
    private IClientNetworkChannel clientChannel;
    private ICoreServerAPI server;
    private ICoreClientAPI client;
    private long serverTickId;
    private long clientTickId;

    private readonly Queue<ServerRequest> serverPending = new();
    private readonly Dictionary<string, HashSet<long>> servedByPlayer = new();

    private readonly Dictionary<long, Vec3i> loadedRegions = new();
    private readonly Dictionary<long, Vec3i> desiredTiles = new();
    private readonly Queue<long> requestQueue = new();
    private readonly HashSet<long> queuedRequests = new();
    private readonly HashSet<long> requestedTiles = new();
    private readonly Dictionary<long, IncomingTile> incomingTiles = new();
    private readonly Dictionary<long, long> requestTimes = new();

    public override void StartServerSide(ICoreServerAPI api)
    {
        server = api;
        serverChannel = api.Network.RegisterChannel("earthclimate")
            .RegisterMessageType<ClimateTileRequest>()
            .RegisterMessageType<ClimateTileChunk>()
            .SetMessageHandler<ClimateTileRequest>(ReceiveRequest);
        serverTickId = api.Event.RegisterGameTickListener(SendPending, 50);
        api.Event.PlayerDisconnect += OnPlayerDisconnect;
    }

    public override void StartClientSide(ICoreClientAPI api)
    {
        client = api;
        clientChannel = api.Network.RegisterChannel("earthclimate")
            .RegisterMessageType<ClimateTileRequest>()
            .RegisterMessageType<ClimateTileChunk>()
            .SetMessageHandler<ClimateTileChunk>(ReceiveTileChunk);
        api.Event.ChunkDirty += OnChunkDirty;
        api.Event.MapRegionUnloaded += OnMapRegionUnloaded;
        api.Event.LevelFinalize += OnLevelFinalize;
        api.Event.LeaveWorld += OnLeaveWorld;
        api.Event.LeftWorld += OnLeftWorld;
        clientTickId = api.Event.RegisterGameTickListener(ClientTick, 100);
    }

    private void ReceiveRequest(IServerPlayer player, ClimateTileRequest request)
    {
        if (player == null || request == null || EarthMapCreator.ClimateData == null) return;
        long key = TileKey(request.RegionX, request.RegionZ);
        string playerUid = player.PlayerUID;

        if (request.Release)
        {
            if (servedByPlayer.TryGetValue(playerUid, out HashSet<long> served)) served.Remove(key);
            return;
        }

        if (player.ConnectionState != EnumClientState.Playing || !ValidTile(request.RegionX, request.RegionZ)) return;
        int chunkSize = server.WorldManager.ChunkSize;
        int mapSizeYChunks = (server.WorldManager.MapSizeY + chunkSize - 1) / chunkSize;
        if (request.WitnessChunkX < 0 || request.WitnessChunkZ < 0
            || request.WitnessChunkY < 0 || request.WitnessChunkY >= mapSizeYChunks
            || (long)request.WitnessChunkX * chunkSize >= EarthMapCreator.ClimateData.MapWidth
            || (long)request.WitnessChunkZ * chunkSize >= EarthMapCreator.ClimateData.MapHeight)
            return;

        int witnessRegionX = (int)((long)request.WitnessChunkX * chunkSize / EarthClimate.TileWorldSize);
        int witnessRegionZ = (int)((long)request.WitnessChunkZ * chunkSize / EarthClimate.TileWorldSize);
        if (Math.Abs((long)request.RegionX - witnessRegionX) > 1
            || Math.Abs((long)request.RegionZ - witnessRegionZ) > 1
            || !server.WorldManager.HasChunk(request.WitnessChunkX, request.WitnessChunkY,
                request.WitnessChunkZ, player))
            return;

        if (!servedByPlayer.TryGetValue(playerUid, out HashSet<long> playerTiles))
        {
            playerTiles = new HashSet<long>();
            servedByPlayer.Add(playerUid, playerTiles);
        }
        if (playerTiles.Count >= MaxServedTilesPerPlayer || !playerTiles.Add(key)) return;
        serverPending.Enqueue(new ServerRequest(player, request, key));
    }

    private void SendPending(float dt)
    {
        for (int sent = 0; sent < MaxRequestsPerTick && serverPending.Count > 0;)
        {
            ServerRequest pending = serverPending.Dequeue();
            if (!servedByPlayer.TryGetValue(pending.Player.PlayerUID, out HashSet<long> served)
                || !served.Contains(pending.Key))
                continue;

            ClimateTileRequest request = pending.Request;
            if (pending.Player.ConnectionState != EnumClientState.Playing
                || !server.WorldManager.HasChunk(request.WitnessChunkX, request.WitnessChunkY,
                    request.WitnessChunkZ, pending.Player))
            {
                served.Remove(pending.Key);
                continue;
            }

            try
            {
                SendTile(pending.Player, request.RegionX, request.RegionZ);
                sent++;
            }
            catch (Exception error)
            {
                served.Remove(pending.Key);
                server.Logger.Error("Failed to send Earth climate tile {0},{1}", request.RegionX, request.RegionZ);
                server.Logger.Error(error);
            }
        }
    }

    private void SendTile(IServerPlayer player, int regionX, int regionZ)
    {
        EarthClimate climate = EarthMapCreator.ClimateData;
        byte[] raw = climate.GetTileBytes(regionX, regionZ);
        using var compressed = new MemoryStream(EarthClimate.TileByteLength);
        using (var gzip = new GZipStream(compressed, CompressionLevel.Fastest, leaveOpen: true))
            gzip.Write(raw);
        int compressedLength = checked((int)compressed.Length);
        if (compressedLength <= 0 || compressedLength > MaxCompressedTileBytes)
            throw new InvalidDataException("Compressed Earth climate tile exceeds its size bound.");

        byte[] buffer = compressed.GetBuffer();
        int count = (compressedLength + PacketBytes - 1) / PacketBytes;
        for (int index = 0; index < count; index++)
        {
            int offset = index * PacketBytes;
            var part = new byte[Math.Min(PacketBytes, compressedLength - offset)];
            Buffer.BlockCopy(buffer, offset, part, 0, part.Length);
            serverChannel.SendPacket(new ClimateTileChunk {
                RegionX = regionX,
                RegionZ = regionZ,
                Index = index,
                Count = count,
                CompressedLength = compressedLength,
                RawLength = EarthClimate.TileByteLength,
                MapWidth = climate.MapWidth,
                MapHeight = climate.MapHeight,
                Spacing = climate.Spacing,
                LapsePerBlock = climate.LapsePerBlock,
                Data = part
            }, player);
        }
    }

    private void OnPlayerDisconnect(IServerPlayer player)
    {
        if (player != null) servedByPlayer.Remove(player.PlayerUID);
    }

    private void OnChunkDirty(Vec3i chunkCoord, IWorldChunk chunk, EnumChunkDirtyReason reason)
    {
        if (reason != EnumChunkDirtyReason.NewlyLoaded && reason != EnumChunkDirtyReason.NewlyCreated) return;
        int chunkSize = GlobalConstants.ChunkSize;
        int maxY = (client.World.BlockAccessor.MapSizeY + chunkSize - 1) / chunkSize;
        if (chunkCoord.X < 0 || chunkCoord.Z < 0 || chunkCoord.Y < 0 || chunkCoord.Y >= maxY
            || (long)chunkCoord.X * chunkSize >= client.World.BlockAccessor.MapSizeX
            || (long)chunkCoord.Z * chunkSize >= client.World.BlockAccessor.MapSizeZ)
            return;

        int regionX = (int)((long)chunkCoord.X * chunkSize / EarthClimate.TileWorldSize);
        int regionZ = (int)((long)chunkCoord.Z * chunkSize / EarthClimate.TileWorldSize);
        long key = TileKey(regionX, regionZ);
        bool firstChunk = !loadedRegions.ContainsKey(key);
        loadedRegions[key] = new Vec3i(chunkCoord.X, chunkCoord.Y, chunkCoord.Z);
        if (firstChunk) ReconcileDesiredTiles();
    }

    private void OnMapRegionUnloaded(Vec2i mapCoord, IMapRegion region)
    {
        if (loadedRegions.Remove(TileKey(mapCoord.X, mapCoord.Y))) ReconcileDesiredTiles();
    }

    private void OnLevelFinalize()
    {
        BootstrapPlayerRegion();
        ReconcileDesiredTiles();
    }

    private void ClientTick(float dt)
    {
        BootstrapPlayerRegion();
        long now = Environment.TickCount64;
        List<long> timedOut = null;
        foreach (long key in requestedTiles)
        {
            if (requestTimes.TryGetValue(key, out long requestedAt) && now - requestedAt >= 10000)
                (timedOut ??= new List<long>()).Add(key);
        }
        if (timedOut != null)
        {
            foreach (long key in timedOut)
            {
                incomingTiles.Remove(key);
                requestedTiles.Remove(key);
                requestTimes.Remove(key);
                if (clientChannel?.Connected == true)
                    clientChannel.SendPacket(new ClimateTileRequest { RegionX = RegionX(key), RegionZ = RegionZ(key), Release = true });
                QueueRequest(key);
            }
        }
        if (clientChannel == null || !clientChannel.Connected) return;

        for (int sent = 0; sent < MaxRequestsPerTick && requestQueue.Count > 0
            && incomingTiles.Count < MaxPendingTransfers;)
        {
            long key = requestQueue.Dequeue();
            queuedRequests.Remove(key);
            if (!desiredTiles.TryGetValue(key, out Vec3i witness) || !requestedTiles.Add(key)) continue;
            requestTimes[key] = now;
            DecodeTileKey(key, out int regionX, out int regionZ);
            clientChannel.SendPacket(new ClimateTileRequest {
                RegionX = regionX,
                RegionZ = regionZ,
                WitnessChunkX = witness.X,
                WitnessChunkY = witness.Y,
                WitnessChunkZ = witness.Z
            });
            sent++;
        }
    }

    private void BootstrapPlayerRegion()
    {
        if (client?.World?.Player?.Entity == null) return;
        BlockPos position = client.World.Player.Entity.Pos.AsBlockPos;
        int chunkSize = GlobalConstants.ChunkSize;
        int chunkX = position.X / chunkSize;
        int chunkY = position.Y / chunkSize;
        int chunkZ = position.Z / chunkSize;
        if (chunkX < 0 || chunkY < 0 || chunkZ < 0
            || client.World.BlockAccessor.GetChunk(chunkX, chunkY, chunkZ) == null)
            return;

        int regionX = position.X / EarthClimate.TileWorldSize;
        int regionZ = position.Z / EarthClimate.TileWorldSize;
        long key = TileKey(regionX, regionZ);
        var witness = new Vec3i(chunkX, chunkY, chunkZ);
        if (loadedRegions.TryGetValue(key, out Vec3i previous)
            && previous.X == chunkX && previous.Y == chunkY && previous.Z == chunkZ) return;
        loadedRegions[key] = witness;
        ReconcileDesiredTiles();
    }

    private void ReconcileDesiredTiles()
    {
        var next = new Dictionary<long, Vec3i>();
        foreach (KeyValuePair<long, Vec3i> loaded in loadedRegions)
        {
            DecodeTileKey(loaded.Key, out int loadedX, out int loadedZ);
            for (int offsetZ = -1; offsetZ <= 1; offsetZ++)
            for (int offsetX = -1; offsetX <= 1; offsetX++)
            {
                int regionX = loadedX + offsetX;
                int regionZ = loadedZ + offsetZ;
                if (!ValidClientTile(regionX, regionZ)) continue;
                next.TryAdd(TileKey(regionX, regionZ), loaded.Value);
            }
        }

        if (next.Count > EarthClimate.MaxClientCachedTiles) TrimToPlayer(next);

        foreach (long key in new List<long>(desiredTiles.Keys))
        {
            if (next.ContainsKey(key)) continue;
            desiredTiles.Remove(key);
            queuedRequests.Remove(key);
            incomingTiles.Remove(key);
            requestTimes.Remove(key);
            EarthMapCreator.ClientClimateData?.RemoveTile(RegionX(key), RegionZ(key));
            if (requestedTiles.Remove(key) && clientChannel?.Connected == true)
                clientChannel.SendPacket(new ClimateTileRequest { RegionX = RegionX(key), RegionZ = RegionZ(key), Release = true });
        }

        foreach (KeyValuePair<long, Vec3i> tile in next)
        {
            desiredTiles[tile.Key] = tile.Value;
            if (requestedTiles.Contains(tile.Key) || queuedRequests.Contains(tile.Key)
                || EarthMapCreator.ClientClimateData?.HasTile(RegionX(tile.Key), RegionZ(tile.Key)) == true)
                continue;
            queuedRequests.Add(tile.Key);
            requestQueue.Enqueue(tile.Key);
        }
    }

    private void TrimToPlayer(Dictionary<long, Vec3i> tiles)
    {
        int centerX = client?.World?.Player?.Entity == null ? 0
            : (int)client.World.Player.Entity.Pos.X / EarthClimate.TileWorldSize;
        int centerZ = client?.World?.Player?.Entity == null ? 0
            : (int)client.World.Player.Entity.Pos.Z / EarthClimate.TileWorldSize;
        var ordered = new List<long>(tiles.Keys);
        ordered.Sort((a, b) => TileDistance(a, centerX, centerZ).CompareTo(TileDistance(b, centerX, centerZ)));
        for (int index = EarthClimate.MaxClientCachedTiles; index < ordered.Count; index++) tiles.Remove(ordered[index]);
    }

    private void ReceiveTileChunk(ClimateTileChunk packet)
    {
        if (!ValidPacket(packet))
        {
            client.Logger.Warning("Ignored invalid Earth climate tile packet");
            return;
        }

        long key = TileKey(packet.RegionX, packet.RegionZ);
        if (!desiredTiles.ContainsKey(key) || !requestedTiles.Contains(key)) return;
        if (!incomingTiles.TryGetValue(key, out IncomingTile incoming))
        {
            if (incomingTiles.Count >= MaxPendingTransfers) return;
            incoming = new IncomingTile(packet);
            incomingTiles.Add(key, incoming);
        }
        else if (!incoming.Matches(packet))
        {
            incomingTiles.Remove(key);
            requestTimes.Remove(key);
            requestedTiles.Remove(key);
            QueueRequest(key);
            return;
        }

        if (incoming.Received[packet.Index]) return;
        int offset = packet.Index * PacketBytes;
        Buffer.BlockCopy(packet.Data, 0, incoming.Compressed, offset, packet.Data.Length);
        incoming.Received[packet.Index] = true;
        incoming.ReceivedCount++;
        if (incoming.ReceivedCount != incoming.Received.Length) return;
        incomingTiles.Remove(key);
        requestTimes.Remove(key);

        try
        {
            byte[] raw = Decompress(incoming.Compressed, packet.RawLength);
            EarthClimate climate = EarthMapCreator.ClientClimateData;
            if (climate == null)
            {
                climate = EarthClimate.CreateTiledClient(packet.MapWidth, packet.MapHeight,
                    packet.Spacing, packet.LapsePerBlock);
                EarthMapCreator.ClientClimateData = climate;
            }
            else if (!climate.MetadataMatches(packet.MapWidth, packet.MapHeight, packet.Spacing, packet.LapsePerBlock))
                throw new InvalidDataException("Earth climate tile metadata changed mid-session.");

            if (desiredTiles.ContainsKey(key))
            {
                climate.AddTile(packet.RegionX, packet.RegionZ, raw);
                RetessellateTile(packet.RegionX, packet.RegionZ);
            }
        }
        catch (Exception error)
        {
            requestedTiles.Remove(key);
            requestTimes.Remove(key);
            QueueRequest(key);
            client.Logger.Error("Failed to receive Earth climate tile {0},{1}", packet.RegionX, packet.RegionZ);
            client.Logger.Error(error);
        }
    }

    // Chunks can be meshed before their climate request completes. Their packed
    // vanilla temperature otherwise remains on screen even in warm weather.
    private void RetessellateTile(int regionX, int regionZ)
    {
        int chunkSize = GlobalConstants.ChunkSize;
        int firstX = regionX * EarthClimate.TileWorldSize / chunkSize;
        int firstZ = regionZ * EarthClimate.TileWorldSize / chunkSize;
        int endX = Math.Min(firstX + EarthClimate.TileWorldSize / chunkSize,
            (client.World.BlockAccessor.MapSizeX + chunkSize - 1) / chunkSize);
        int endZ = Math.Min(firstZ + EarthClimate.TileWorldSize / chunkSize,
            (client.World.BlockAccessor.MapSizeZ + chunkSize - 1) / chunkSize);
        int endY = (client.World.BlockAccessor.MapSizeY + chunkSize - 1) / chunkSize;
        for (int z = firstZ; z < endZ; z++)
        for (int x = firstX; x < endX; x++)
        for (int y = 0; y < endY; y++)
        {
            if (client.World.BlockAccessor.GetChunk(x, y, z) != null)
                ((Vintagestory.Client.NoObf.ClientWorldMap)client.World.ChunkProvider)
                    .MarkChunkDirty(x, y, z, fireEvent: false);
        }
    }

    private bool ValidPacket(ClimateTileChunk packet)
    {
        if (packet == null || packet.RawLength != EarthClimate.TileByteLength
            || packet.CompressedLength <= 0 || packet.CompressedLength > MaxCompressedTileBytes
            || packet.Count != (packet.CompressedLength + PacketBytes - 1) / PacketBytes
            || packet.Index < 0 || packet.Index >= packet.Count || packet.Data == null)
            return false;
        int expectedPartLength = Math.Min(PacketBytes, packet.CompressedLength - packet.Index * PacketBytes);
        if (packet.Data.Length != expectedPartLength || packet.MapWidth != client.World.BlockAccessor.MapSizeX
            || packet.MapHeight != client.World.BlockAccessor.MapSizeZ || packet.Spacing != 8
            || !float.IsFinite(packet.LapsePerBlock) || packet.LapsePerBlock > 0)
            return false;
        return ValidClientTile(packet.RegionX, packet.RegionZ);
    }

    private static byte[] Decompress(byte[] compressed, int rawLength)
    {
        var raw = GC.AllocateUninitializedArray<byte>(rawLength);
        using var source = new MemoryStream(compressed, writable: false);
        using var gzip = new GZipStream(source, CompressionMode.Decompress);
        int offset = 0;
        while (offset < raw.Length)
        {
            int count = gzip.Read(raw, offset, raw.Length - offset);
            if (count == 0) throw new InvalidDataException("Truncated Earth climate tile.");
            offset += count;
        }
        if (gzip.ReadByte() >= 0) throw new InvalidDataException("Earth climate tile length mismatch.");
        return raw;
    }

    private void QueueRequest(long key)
    {
        if (!desiredTiles.ContainsKey(key) || requestedTiles.Contains(key) || !queuedRequests.Add(key)) return;
        requestQueue.Enqueue(key);
    }

    private bool ValidTile(int regionX, int regionZ)
    {
        EarthClimate climate = EarthMapCreator.ClimateData;
        return regionX >= 0 && regionZ >= 0
            && (long)regionX * EarthClimate.TileWorldSize < climate.MapWidth
            && (long)regionZ * EarthClimate.TileWorldSize < climate.MapHeight;
    }

    private bool ValidClientTile(int regionX, int regionZ) =>
        regionX >= 0 && regionZ >= 0
        && (long)regionX * EarthClimate.TileWorldSize < client.World.BlockAccessor.MapSizeX
        && (long)regionZ * EarthClimate.TileWorldSize < client.World.BlockAccessor.MapSizeZ;

    private void OnLeaveWorld() => ClearClientState();
    private void OnLeftWorld() => ClearClientState();

    private void ClearClientState()
    {
        EarthMapCreator.ClientClimateData?.Dispose();
        EarthMapCreator.ClientClimateData = null;
        loadedRegions.Clear();
        desiredTiles.Clear();
        requestQueue.Clear();
        queuedRequests.Clear();
        requestedTiles.Clear();
        requestTimes.Clear();
        incomingTiles.Clear();
    }

    private static long TileKey(int regionX, int regionZ) => ((long)(uint)regionX << 32) | (uint)regionZ;
    private static int RegionX(long key) => unchecked((int)(key >> 32));
    private static int RegionZ(long key) => unchecked((int)key);
    private static void DecodeTileKey(long key, out int regionX, out int regionZ)
    {
        regionX = RegionX(key);
        regionZ = RegionZ(key);
    }
    private static long TileDistance(long key, int centerX, int centerZ)
    {
        long dx = (long)RegionX(key) - centerX;
        long dz = (long)RegionZ(key) - centerZ;
        return dx * dx + dz * dz;
    }

    public override void Dispose()
    {
        if (server != null)
        {
            server.Event.PlayerDisconnect -= OnPlayerDisconnect;
            server.Event.UnregisterGameTickListener(serverTickId);
        }
        if (client != null)
        {
            client.Event.ChunkDirty -= OnChunkDirty;
            client.Event.MapRegionUnloaded -= OnMapRegionUnloaded;
            client.Event.LevelFinalize -= OnLevelFinalize;
            client.Event.LeaveWorld -= OnLeaveWorld;
            client.Event.LeftWorld -= OnLeftWorld;
            client.Event.UnregisterGameTickListener(clientTickId);
            ClearClientState();
        }
        serverPending.Clear();
        servedByPlayer.Clear();
    }

    private sealed class IncomingTile
    {
        internal readonly byte[] Compressed;
        internal readonly bool[] Received;
        internal readonly int MapWidth;
        internal readonly int MapHeight;
        internal readonly int Spacing;
        internal readonly float LapsePerBlock;
        internal int ReceivedCount;

        internal IncomingTile(ClimateTileChunk packet)
        {
            Compressed = GC.AllocateUninitializedArray<byte>(packet.CompressedLength);
            Received = new bool[packet.Count];
            MapWidth = packet.MapWidth;
            MapHeight = packet.MapHeight;
            Spacing = packet.Spacing;
            LapsePerBlock = packet.LapsePerBlock;
        }

        internal bool Matches(ClimateTileChunk packet) =>
            Compressed.Length == packet.CompressedLength && Received.Length == packet.Count
            && MapWidth == packet.MapWidth && MapHeight == packet.MapHeight && Spacing == packet.Spacing
            && BitConverter.SingleToInt32Bits(LapsePerBlock) == BitConverter.SingleToInt32Bits(packet.LapsePerBlock);
    }

    private readonly struct ServerRequest
    {
        internal readonly IServerPlayer Player;
        internal readonly ClimateTileRequest Request;
        internal readonly long Key;

        internal ServerRequest(IServerPlayer player, ClimateTileRequest request, long key)
        {
            Player = player;
            Request = request;
            Key = key;
        }
    }
}
