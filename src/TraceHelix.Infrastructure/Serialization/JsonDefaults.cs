using System.Text.Json;
using System.Text.Json.Serialization;
namespace TraceHelix.Infrastructure.Serialization;

public static class JsonDefaults
{
    public static JsonSerializerOptions Options { get; } = Create();
    private static JsonSerializerOptions Create() { var o = new JsonSerializerOptions(JsonSerializerDefaults.Web) { WriteIndented = true }; o.Converters.Add(new JsonStringEnumConverter()); return o; }
}
