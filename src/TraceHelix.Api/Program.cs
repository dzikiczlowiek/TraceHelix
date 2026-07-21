using Microsoft.AspNetCore.HostFiltering;
using TraceHelix.Api;
using TraceHelix.Api.Endpoints;
using TraceHelix.Application;
using TraceHelix.Application.Abstractions;
using TraceHelix.Infrastructure.Classification;
using TraceHelix.Infrastructure.Detection;
using TraceHelix.Infrastructure.Persistence;

var builder = WebApplication.CreateBuilder(args);
LoopbackUrlValidator.RejectKestrelEndpointOverrides(builder.Configuration);
var wildcardRequested = bool.TryParse(builder.Configuration["TRACEHELIX_ALLOW_WILDCARD"], out var configuredWildcard) && configuredWildcard;
var allowWildcard = LoopbackUrlValidator.AllowWildcard(wildcardRequested, LoopbackUrlValidator.IsContainerRuntime());
builder.WebHost.UseUrls(LoopbackUrlValidator.Validate(builder.Configuration["URLS"], allowWildcard));
builder.Services.Configure<HostFilteringOptions>(options =>
    options.AllowedHosts = ["localhost", "127.0.0.1", "[::1]"]);
builder.Services.AddOpenApi("v1");
builder.Services.AddProblemDetails(options => options.CustomizeProblemDetails = context =>
{
    context.ProblemDetails.Extensions.Remove("exception");
    if (context.ProblemDetails.Status == 500) context.ProblemDetails.Detail = "An unexpected error occurred.";
});
var database = builder.Configuration["TRACEHELIX_DB"] ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "TraceHelix", "tracehelix.db");
builder.Services.AddSingleton(new SqliteStore(database));
builder.Services.AddSingleton<ITraceRepository>(s => s.GetRequiredService<SqliteStore>());
builder.Services.AddSingleton<IAnalysisRepository>(s => s.GetRequiredService<SqliteStore>());
builder.Services.AddSingleton<IStepClassifier, RuleBasedStepClassifier>();
builder.Services.AddSingleton(DefaultDetectors.Create());
builder.Services.AddTransient<AnalyzeRun>();
builder.Services.AddTransient<CompareRuns>();
var developmentOrigins = new[] { "http://127.0.0.1:5173", "http://localhost:5173" };
if (builder.Environment.IsDevelopment()) builder.Services.AddCors(o => o.AddDefaultPolicy(p => p.WithOrigins(developmentOrigins).AllowAnyHeader().AllowAnyMethod()));

var app = builder.Build();
app.UseHostFiltering();
app.Use(async (context, next) =>
{
    if (!HttpMethods.IsGet(context.Request.Method) &&
        !HttpMethods.IsHead(context.Request.Method) &&
        !HttpMethods.IsOptions(context.Request.Method) &&
        context.Request.Headers.TryGetValue("Origin", out var origins))
    {
        var expectedOrigin = $"{context.Request.Scheme}://{context.Request.Host}";
        var origin = origins.Count == 1 ? origins[0] : null;
        var allowedDevelopmentOrigin = app.Environment.IsDevelopment() &&
            origin is not null &&
            developmentOrigins.Contains(origin, StringComparer.OrdinalIgnoreCase);
        if (origin is null ||
            (!string.Equals(origin, expectedOrigin, StringComparison.OrdinalIgnoreCase) && !allowedDevelopmentOrigin))
        {
            context.Response.StatusCode = StatusCodes.Status403Forbidden;
            return;
        }
    }

    await next(context);
});
app.UseExceptionHandler();
if (app.Environment.IsDevelopment())
{
    app.UseCors();
    app.MapOpenApi();
}
app.MapTraceHelixApi();
app.Run();

public partial class Program;
