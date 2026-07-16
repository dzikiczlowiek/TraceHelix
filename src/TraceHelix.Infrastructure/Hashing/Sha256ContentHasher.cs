using System.Security.Cryptography;
namespace TraceHelix.Infrastructure.Hashing;

public static class Sha256ContentHasher
{
    public static async Task<string> HashFileAsync(string path, CancellationToken cancellationToken) { await using var stream = File.OpenRead(path); var hash = await SHA256.HashDataAsync(stream, cancellationToken); return Convert.ToHexStringLower(hash); }
    public static string Hash(ReadOnlySpan<byte> bytes) => Convert.ToHexStringLower(SHA256.HashData(bytes));
}
