using System;
using System.IO;
using System.IO.Compression;
using ProtoBuf;
using Vintagestory.API.Client;
using Vintagestory.API.Common;
using Vintagestory.API.Server;

namespace EarthMapCreator;

[ProtoContract]
public sealed class ClimateChunk
{
    [ProtoMember(1)] public int Index;
    [ProtoMember(2)] public int Count;
    [ProtoMember(3)] public int RawLength;
    [ProtoMember(4)] public int Width;
    [ProtoMember(5)] public int Height;
    [ProtoMember(6)] public byte[] Data = Array.Empty<byte>();
}

/// <summary>Synchronizes the authoritative climate file on join; never requires
/// clients to download the region store or install maps locally.</summary>
public sealed class ClimateSync : ModSystem
{
    private const int ChunkSize = 32768;
    private const int MaxBytes = 256 * 1024 * 1024;
    private IServerNetworkChannel serverChannel;
    private ICoreServerAPI server;
    private ICoreClientAPI client;
    private byte[] compressed;
    private int rawLength;
    private byte[][] received;
    private int receivedCount;
    private int receivedLength;
    private int width;
    private int height;
    private long tickId;
    private readonly System.Collections.Generic.Queue<(IServerPlayer player, int index)> pending = new();

    public override void StartServerSide(ICoreServerAPI api)
    {
        server = api;
        serverChannel = api.Network.RegisterChannel("earthclimate").RegisterMessageType<ClimateChunk>();
        api.Event.PlayerNowPlaying += OnPlayerReady;
        tickId = api.Event.RegisterGameTickListener(SendPending, 50);
        rawLength = EarthMapCreator.ClimateData.GetTransferBytes().Length;
        using var stream = new MemoryStream();
        using (var gzip = new GZipStream(stream, CompressionLevel.Fastest, leaveOpen: true))
            gzip.Write(EarthMapCreator.ClimateData.GetTransferBytes());
        compressed = stream.ToArray();
    }

    public override void StartClientSide(ICoreClientAPI api)
    {
        client = api;
        api.Network.RegisterChannel("earthclimate").RegisterMessageType<ClimateChunk>().SetMessageHandler<ClimateChunk>(Receive);
    }

    private void OnPlayerReady(IServerPlayer player)
    {
        int count = (compressed.Length + ChunkSize - 1) / ChunkSize;
        for (int i = 0; i < count; i++) pending.Enqueue((player, i));
    }

    private void SendPending(float dt)
    {
        int count = (compressed.Length + ChunkSize - 1) / ChunkSize;
        for (int i = 0; i < 8 && pending.Count > 0; i++)
        {
            var (player, index) = pending.Dequeue();
            int offset = index * ChunkSize;
            byte[] part = new byte[Math.Min(ChunkSize, compressed.Length - offset)];
            Buffer.BlockCopy(compressed, offset, part, 0, part.Length);
            serverChannel.SendPacket(new ClimateChunk {
                Index = index, Count = count, RawLength = rawLength,
                Width = EarthMapCreator.ClimateData.MapWidth, Height = EarthMapCreator.ClimateData.MapHeight,
                Data = part
            }, player);
        }
    }

    private void Receive(ClimateChunk chunk)
    {
        if (chunk.Count <= 0 || chunk.Count > MaxBytes / ChunkSize || chunk.Index < 0 || chunk.Index >= chunk.Count
            || chunk.RawLength <= 0 || chunk.RawLength > MaxBytes || chunk.Data == null || chunk.Data.Length == 0 || chunk.Data.Length > ChunkSize
            || chunk.Width <= 0 || chunk.Height <= 0) throw new InvalidDataException("Invalid climate transfer packet");
        if (received == null)
        {
            received = new byte[chunk.Count][];
            receivedCount = 0;
            receivedLength = 0;
            rawLength = chunk.RawLength;
            width = chunk.Width;
            height = chunk.Height;
            EarthMapCreator.ClientClimateData = null;
        }
        else if (chunk.Index == 0 && received[0] != null) return;
        if (chunk.Count != received.Length || chunk.RawLength != rawLength || chunk.Width != width || chunk.Height != height)
            throw new InvalidDataException("Climate transfer changed mid-stream");
        if (received[chunk.Index] != null) return;
        received[chunk.Index] = chunk.Data;
        receivedCount++;
        receivedLength += chunk.Data.Length;
        if (receivedLength > MaxBytes) throw new InvalidDataException("Climate transfer exceeds size limit");
        if (receivedCount != received.Length) return;

        using var source = new MemoryStream(receivedLength);
        foreach (var part in received) source.Write(part);
        source.Position = 0;
        byte[] raw = new byte[rawLength];
        using (var gzip = new GZipStream(source, CompressionMode.Decompress))
        {
            int offset = 0;
            while (offset < rawLength)
            {
                int got = gzip.Read(raw, offset, rawLength - offset);
                if (got == 0) throw new InvalidDataException("Truncated climate transfer");
                offset += got;
            }
            if (gzip.ReadByte() >= 0) throw new InvalidDataException("Climate transfer length mismatch");
        }
        EarthMapCreator.ClientClimateData = new EarthClimate(raw, width, height);
        received = null;
        client?.Logger.Notification("Loaded Earth climate from server ({0} x {1})", width, height);
    }

    public override void Dispose()
    {
        if (server != null)
        {
            server.Event.PlayerNowPlaying -= OnPlayerReady;
            server.Event.UnregisterGameTickListener(tickId);
        }
        pending.Clear();
        received = null;
        if (client != null) EarthMapCreator.ClientClimateData = null;
    }
}
