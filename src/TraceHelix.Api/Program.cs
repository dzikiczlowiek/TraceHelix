using TraceHelix.Api;
using TraceHelix.Api.Endpoints;
using TraceHelix.Application;
using TraceHelix.Application.Abstractions;
using TraceHelix.Infrastructure.Classification;
using TraceHelix.Infrastructure.Detection;
using TraceHelix.Infrastructure.Persistence;

var builder = WebApplication.CreateBuilder(args);
builder.WebHost.UseUrls(LoopbackUrlValidator.Validate(builder.Configuration["URLS"]));
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
if (builder.Environment.IsDevelopment()) builder.Services.AddCors(o => o.AddDefaultPolicy(p => p.WithOrigins("http://127.0.0.1:5173", "http://localhost:5173").AllowAnyHeader().AllowAnyMethod()));

var app = builder.Build();
app.UseExceptionHandler();
if (app.Environment.IsDevelopment())
{
    app.UseCors();
    app.MapOpenApi();
}
app.MapTraceHelixApi();
app.Run();

public partial class Program;
