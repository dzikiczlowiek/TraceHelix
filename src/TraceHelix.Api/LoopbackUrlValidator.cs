using System.Net;
using Microsoft.Extensions.Configuration;

namespace TraceHelix.Api;

public static class LoopbackUrlValidator
{
    public const string DefaultUrl = "http://127.0.0.1:5080";

    public static bool AllowWildcard(bool requested, bool isContainerRuntime) =>
        requested && isContainerRuntime;

    public static bool IsContainerRuntime() =>
        File.Exists("/.dockerenv") || File.Exists("/run/.containerenv");

    public static string Validate(string? configuredUrls, bool allowWildcard = false)
    {
        var value = string.IsNullOrWhiteSpace(configuredUrls) ? DefaultUrl : configuredUrls;
        var urls = value.Split(';', StringSplitOptions.TrimEntries);
        if (urls.Length == 0 || urls.Any(url => !IsAllowed(url, allowWildcard)))
            throw new InvalidOperationException("Invalid listen URL configuration: every URL must use http or https, an explicit valid port, and a literal loopback address or localhost. Configured values were redacted.");
        return value;
    }

    public static void RejectKestrelEndpointOverrides(IConfiguration configuration)
    {
        var endpoints = configuration.GetSection("Kestrel:Endpoints");
        if (endpoints.Value is not null || endpoints.GetChildren().Any())
        {
            throw new InvalidOperationException(
                "Kestrel endpoint configuration is not supported because the API listener must remain loopback-only.");
        }
    }

    private static bool IsAllowed(string value, bool allowWildcard)
    {
        if (string.IsNullOrWhiteSpace(value) || !Uri.TryCreate(value, UriKind.Absolute, out var uri)) return false;
        if (uri.Scheme is not ("http" or "https") || uri.Port is < 1 or > 65535 ||
            uri.UserInfo.Length != 0 || uri.AbsolutePath != "/" || uri.Query.Length != 0 || uri.Fragment.Length != 0)
            return false;

        // Require an explicit port (Uri supplies scheme defaults when one is omitted).
        var authority = value[(value.IndexOf("://", StringComparison.Ordinal) + 3)..];
        var slash = authority.IndexOfAny(['/', '?', '#']);
        if (slash >= 0) authority = authority[..slash];
        var hasPort = authority.StartsWith("[", StringComparison.Ordinal)
            ? authority.Contains("]:", StringComparison.Ordinal)
            : authority.Count(c => c == ':') == 1;
        if (!hasPort) return false;

        if (string.Equals(uri.Host, "localhost", StringComparison.OrdinalIgnoreCase)) return true;
        var host = uri.Host.Trim('[', ']');
        return IPAddress.TryParse(host, out var address) &&
            (IPAddress.IsLoopback(address) ||
             (allowWildcard && (address.Equals(IPAddress.Any) || address.Equals(IPAddress.IPv6Any))));
    }
}
