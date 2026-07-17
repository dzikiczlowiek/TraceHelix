using TraceHelix.Api;
using Xunit;

namespace TraceHelix.Api.Tests;

public sealed class LoopbackUrlValidatorTests
{
    [Theory]
    [InlineData(null, LoopbackUrlValidator.DefaultUrl)]
    [InlineData("", LoopbackUrlValidator.DefaultUrl)]
    [InlineData("http://127.0.0.1:5080", "http://127.0.0.1:5080")]
    [InlineData("https://localhost:7443", "https://localhost:7443")]
    [InlineData("http://[::1]:5080", "http://[::1]:5080")]
    [InlineData("http://127.0.0.1:1;https://localhost:65535", "http://127.0.0.1:1;https://localhost:65535")]
    public void Allows_only_loopback(string? value, string expected) => Assert.Equal(expected, LoopbackUrlValidator.Validate(value));

    [Theory]
    [InlineData("http://0.0.0.0:5080")]
    [InlineData("http://[::]:5080")]
    [InlineData("http://192.168.1.2:5080")]
    [InlineData("https://example.com:443")]
    [InlineData("http://*:5080")]
    [InlineData("http://+:5080")]
    [InlineData("http://localhost:5080;http://10.0.0.2:5080")]
    [InlineData("http://localhost")]
    [InlineData("ftp://localhost:21")]
    public void Rejects_non_loopback_or_invalid_values_without_echoing_them(string value)
    {
        var error = Assert.Throws<InvalidOperationException>(() => LoopbackUrlValidator.Validate(value));
        Assert.Contains("redacted", error.Message, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain(value, error.Message, StringComparison.Ordinal);
    }
}
