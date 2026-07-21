using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Data.Sqlite;
using TraceHelix.Application.Abstractions;
using TraceHelix.Domain.Runs;
using TraceHelix.Domain.Traces;
using TraceHelix.Infrastructure.Persistence;
using Xunit;
namespace TraceHelix.Infrastructure.Tests.Persistence;

public sealed class SqliteStoreTests
{
    [DllImport("libc", EntryPoint = "mkfifo", SetLastError = true)]
    private static extern int MakeFifo(string path, uint mode);

    [Fact]
    public void Repairs_a_persisted_wal_database_to_delete_journal_mode()
    {
        var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        var path = Path.Combine(dir, "tracehelix.db");
        CreatePrivateDirectory(dir);

        using (var connection = new SqliteConnection($"Data Source={path};Pooling=False"))
        {
            connection.Open();
            using var command = connection.CreateCommand();
            command.CommandText = "PRAGMA journal_mode=WAL;";
            Assert.Equal("wal", command.ExecuteScalar()?.ToString(), ignoreCase: true);
        }

        _ = new SqliteStore(path);

        using (var connection = new SqliteConnection($"Data Source={path};Pooling=False"))
        {
            connection.Open();
            using var command = connection.CreateCommand();
            command.CommandText = "PRAGMA journal_mode;";
            Assert.Equal("delete", command.ExecuteScalar()?.ToString(), ignoreCase: true);
        }

        Assert.False(File.Exists(path + "-wal"));
        Assert.False(File.Exists(path + "-shm"));
        Directory.Delete(dir, true);
    }

    [Fact]
    public async Task Revalidates_database_path_before_each_connection()
    {
        var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        CreatePrivateDirectory(dir);
        var main = Path.Combine(dir, "main.db");
        var victim = Path.Combine(dir, "victim.db");
        var store = new SqliteStore(main);
        _ = new SqliteStore(victim);
        File.Delete(main);
        try
        {
            File.CreateSymbolicLink(main, victim);
        }
        catch (Exception linkError) when (linkError is PlatformNotSupportedException or UnauthorizedAccessException)
        {
            Directory.Delete(dir, true);
            Assert.Skip($"File symlinks unavailable: {linkError.Message}");
            return;
        }

        var error = await Record.ExceptionAsync(() => store.ListAsync(TestContext.Current.CancellationToken));

        Assert.IsType<IOException>(error);
        Directory.Delete(dir, true);
    }

    [Fact]
    public async Task Canonicalizes_database_parent_before_later_connections()
    {
        if (OperatingSystem.IsWindows())
        {
            Assert.Skip("Unix filesystem security semantics are exercised on Linux and macOS.");
            return;
        }

        var target = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        var alternate = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        CreatePrivateDirectory(target);
        CreatePrivateDirectory(alternate);
        var alias = Path.Combine(Path.GetTempPath(), $"tracehelix-parent-{Guid.NewGuid():N}");
        Directory.CreateSymbolicLink(alias, target);
        var store = new SqliteStore(Path.Combine(alias, "tracehelix.db"));
        Directory.Delete(alias);
        Directory.CreateSymbolicLink(alias, alternate);

        _ = await store.ListAsync(TestContext.Current.CancellationToken);

        Assert.True(File.Exists(Path.Combine(target, "tracehelix.db")));
        Assert.False(File.Exists(Path.Combine(alternate, "tracehelix.db")));
        Directory.Delete(alias);
        Directory.Delete(target, true);
        Directory.Delete(alternate, true);
    }

    [Fact]
    public void Rejects_non_regular_fifo_without_changing_permissions()
    {
        if (OperatingSystem.IsWindows())
        {
            Assert.Skip("Unix filesystem security semantics are exercised on Linux and macOS.");
            return;
        }

        var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        CreatePrivateDirectory(dir);
        var fifo = Path.Combine(dir, "tracehelix.db");
        Assert.Equal(0, MakeFifo(fifo, 0x01B6)); // 0666 before umask
        const UnixFileMode original = UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.GroupRead | UnixFileMode.GroupWrite;
        File.SetUnixFileMode(fifo, original);

        var error = Record.Exception(() => _ = new SqliteStore(fifo));

        Assert.True(error is IOException or NotSupportedException,
            $"Expected the FIFO to be rejected, but received {error?.GetType().FullName ?? "no exception"}.");
        Assert.Equal(original, File.GetUnixFileMode(fifo));
        Directory.Delete(dir, true);
    }

    [Fact]
    public void Rejects_database_in_group_writable_directory()
    {
        if (OperatingSystem.IsWindows())
        {
            Assert.Skip("Unix filesystem security semantics are exercised on Linux and macOS.");
            return;
        }

        var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        const UnixFileMode writableDirectoryMode = UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute |
            UnixFileMode.GroupWrite | UnixFileMode.GroupExecute;
        Directory.CreateDirectory(dir, writableDirectoryMode);
        File.SetUnixFileMode(dir, writableDirectoryMode);
        try
        {
            Assert.Throws<IOException>(() => _ = new SqliteStore(Path.Combine(dir, "tracehelix.db")));
        }
        finally
        {
            Directory.Delete(dir, true);
        }
    }

    [Fact]
    public void Rejects_database_symlink_before_opening_its_target()
    {
        var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        var target = Path.Combine(dir, "victim");
        var link = Path.Combine(dir, "tracehelix.db");
        CreatePrivateDirectory(dir);
        File.WriteAllBytes(target, []);
        const UnixFileMode originalMode = UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.GroupRead | UnixFileMode.OtherRead;
        if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(target, originalMode);
        try
        {
            File.CreateSymbolicLink(link, target);
        }
        catch (Exception linkError) when (linkError is PlatformNotSupportedException or UnauthorizedAccessException)
        {
            Directory.Delete(dir, true);
            Assert.Skip($"File symlinks unavailable: {linkError.Message}");
            return;
        }

        var error = Record.Exception(() => _ = new SqliteStore(link));

        Assert.IsType<IOException>(error);
        if (!OperatingSystem.IsWindows()) Assert.Equal(originalMode, File.GetUnixFileMode(target));
        Directory.Delete(dir, true);
    }

    [Fact]
    public void Creates_and_repairs_private_database_permissions_on_unix()
    {
        if (OperatingSystem.IsWindows())
        {
            Assert.Skip("Unix filesystem security semantics are exercised on Linux and macOS.");
            return;
        }

        var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        var createdPath = Path.Combine(dir, "created.db");
        var existingPath = Path.Combine(dir, "existing.db");
        CreatePrivateDirectory(dir);
        File.WriteAllBytes(existingPath, []);
        File.SetUnixFileMode(existingPath, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.GroupRead | UnixFileMode.OtherRead);

        _ = new SqliteStore(createdPath);
        _ = new SqliteStore(existingPath);

        const UnixFileMode expected = UnixFileMode.UserRead | UnixFileMode.UserWrite;
        Assert.Equal(expected, File.GetUnixFileMode(createdPath));
        Assert.Equal(expected, File.GetUnixFileMode(existingPath));
        Directory.Delete(dir, true);
    }

    [Fact]
    public async Task Persists_the_same_canonical_import_timestamp_in_column_and_json()
    {
        var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        var path = Path.Combine(dir, "tracehelix.db");
        var importedAt = new DateTimeOffset(2026, 1, 1, 0, 0, 0, TimeSpan.Zero).AddTicks(1_234_560);
        var run = new TraceRun(Guid.NewGuid(), "x", "input", "generic-jsonl", "1.0", importedAt, [], []);
        var store = new SqliteStore(path);

        Assert.True(await store.TrySaveAsync(run, TestContext.Current.CancellationToken));

        await using var connection = new SqliteConnection($"Data Source={path};Mode=ReadOnly;Pooling=False");
        await connection.OpenAsync(TestContext.Current.CancellationToken);
        await using var command = connection.CreateCommand();
        command.CommandText = "SELECT imported_at,data_json FROM runs WHERE id=$id";
        command.Parameters.AddWithValue("$id", run.Id.ToString());
        await using var reader = await command.ExecuteReaderAsync(TestContext.Current.CancellationToken);
        Assert.True(await reader.ReadAsync(TestContext.Current.CancellationToken));
        var columnTimestamp = reader.GetString(0);
        using var document = JsonDocument.Parse(reader.GetString(1));

        Assert.Equal(document.RootElement.GetProperty("importedAt").GetString(), columnTimestamp);
        await reader.DisposeAsync();
        await connection.DisposeAsync();
        Directory.Delete(dir, true);
    }

    [Fact] public async Task Persists_real_sqlite_run_and_enforces_import_identity() { var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N")); var path = Path.Combine(dir, "tracehelix.db"); var runId = Guid.NewGuid(); var e = new TraceEvent(Guid.NewGuid(), runId, 0, DateTimeOffset.UnixEpoch, TraceEventKind.Message, "agent", "hello", JsonDocument.Parse("{}").RootElement, new SourceReference("generic-jsonl", "input", "x", 0, 1, "/"), [], "content"); var run = new TraceRun(runId, "x", "input", "generic-jsonl", "1.0", DateTimeOffset.UnixEpoch, [e], []); var store = new SqliteStore(path); Assert.True(await store.TrySaveAsync(run, TestContext.Current.CancellationToken)); var loaded = await store.GetAsync(runId, TestContext.Current.CancellationToken); Assert.NotNull(loaded); Assert.Single(loaded.Events); Assert.False(await store.TrySaveAsync(run with { Id = Guid.NewGuid() }, TestContext.Current.CancellationToken)); Directory.Delete(dir, true); }

    [Fact]
    public async Task Releases_database_file_after_each_operation()
    {
        var dir = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        var path = Path.Combine(dir, "tracehelix.db");
        var store = new SqliteStore(path);

        _ = await store.ListAsync(TestContext.Current.CancellationToken);

        await using (new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
        {
        }

        File.Delete(path);
        Assert.False(File.Exists(path));
        Directory.Delete(dir);
    }

    private static void CreatePrivateDirectory(string path)
    {
        Directory.CreateDirectory(path);
        if (!OperatingSystem.IsWindows())
            File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
    }
}
