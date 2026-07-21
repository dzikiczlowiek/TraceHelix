using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text.Json;
using Microsoft.Data.Sqlite;
using Microsoft.Win32.SafeHandles;
using TraceHelix.Application.Abstractions;
using TraceHelix.Domain.Analysis;
using TraceHelix.Domain.Runs;
using TraceHelix.Infrastructure.Serialization;

namespace TraceHelix.Infrastructure.Persistence;

public sealed class SqliteStore : ITraceRepository, IAnalysisRepository
{
    private readonly string databasePath;
    private readonly string connectionString;

    [DllImport("libc", EntryPoint = "open", SetLastError = true)]
    private static extern int OpenNoFollow(string path, int flags, uint mode);

    [DllImport("libc", EntryPoint = "realpath", SetLastError = true)]
    private static extern IntPtr RealPath(string path, IntPtr resolvedPath);

    [DllImport("libc", EntryPoint = "free")]
    private static extern void Free(IntPtr pointer);

    public SqliteStore(string databasePath)
    {
        if (string.IsNullOrWhiteSpace(databasePath))
        {
            throw new ArgumentException("Database path is required.", nameof(databasePath));
        }

        var fullPath = Path.GetFullPath(databasePath);
        var directory = Path.GetDirectoryName(fullPath)!;
        if (OperatingSystem.IsWindows())
            Directory.CreateDirectory(directory);
        else
            Directory.CreateDirectory(directory, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        this.databasePath = Path.Combine(CanonicalizeDirectory(directory), Path.GetFileName(fullPath));
        EnsurePrivateDatabaseFile(this.databasePath);
        connectionString = new SqliteConnectionStringBuilder
        {
            DataSource = this.databasePath,
            Mode = SqliteOpenMode.ReadWriteCreate,
            Pooling = false,
        }.ToString();
        Initialize();
    }

    private static string CanonicalizeDirectory(string path)
    {
        if (OperatingSystem.IsWindows()) return path;

        var resolved = RealPath(path, IntPtr.Zero);
        if (resolved == IntPtr.Zero)
            throw new IOException("The database directory could not be resolved securely.", new Win32Exception(Marshal.GetLastPInvokeError()));

        try
        {
            return Marshal.PtrToStringUTF8(resolved)
                ?? throw new IOException("The database directory could not be resolved securely.");
        }
        finally
        {
            Free(resolved);
        }
    }

    private static void EnsurePrivateDatabaseFile(string path)
    {
        if (OperatingSystem.IsWindows())
        {
            if (new FileInfo(path).LinkTarget is not null)
                throw new IOException("The database path must not be a symbolic link.");
            return;
        }

        const UnixFileMode untrustedDirectoryWrite = UnixFileMode.GroupWrite | UnixFileMode.OtherWrite;
        var directory = Path.GetDirectoryName(path)!;
        if ((File.GetUnixFileMode(directory) & untrustedDirectoryWrite) != 0)
            throw new IOException("The database directory must not be writable by group or other users.");

        const int readWrite = 0x0002;
        const uint privateMode = 0x0180; // 0600
        int flags;
        if (OperatingSystem.IsLinux())
            flags = readWrite | 0x0040 | 0x20000 | 0x80000; // O_CREAT | O_NOFOLLOW | O_CLOEXEC
        else if (OperatingSystem.IsMacOS())
            flags = readWrite | 0x0200 | 0x0100 | 0x1000000; // O_CREAT | O_NOFOLLOW | O_CLOEXEC
        else
            throw new PlatformNotSupportedException("Secure SQLite file creation requires Windows, Linux, or macOS.");

        var descriptor = OpenNoFollow(path, flags, privateMode);
        if (descriptor < 0)
            throw new IOException("The database path must resolve directly to a regular file.", new Win32Exception(Marshal.GetLastPInvokeError()));

        using var handle = new SafeFileHandle((IntPtr)descriptor, ownsHandle: true);
        try
        {
            _ = RandomAccess.GetLength(handle);
        }
        catch (IOException error)
        {
            throw new IOException("The database path must resolve directly to a regular file.", error);
        }

        File.SetUnixFileMode(handle, UnixFileMode.UserRead | UnixFileMode.UserWrite);
    }

    public async Task<bool> TrySaveAsync(TraceRun run, CancellationToken cancellationToken)
    {
        await using var connection = Open();
        await using var transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
        await using (var command = connection.CreateCommand())
        {
            command.Transaction = transaction;
            command.CommandText = "INSERT INTO runs(id,input_hash,adapter,adapter_version,imported_at,data_json) VALUES($id,$hash,$adapter,$version,$at,$json) ON CONFLICT(input_hash,adapter,adapter_version) DO NOTHING";
            command.Parameters.AddWithValue("$id", run.Id.ToString());
            command.Parameters.AddWithValue("$hash", run.InputSha256);
            command.Parameters.AddWithValue("$adapter", run.Adapter);
            command.Parameters.AddWithValue("$version", run.AdapterVersion);
            var importedAt = JsonSerializer.SerializeToElement(run.ImportedAt, JsonDefaults.Options).GetString()
                ?? throw new InvalidOperationException("Imported timestamp serialization failed.");
            command.Parameters.AddWithValue("$at", importedAt);
            command.Parameters.AddWithValue("$json", JsonSerializer.Serialize(run, JsonDefaults.Options));
            if (await command.ExecuteNonQueryAsync(cancellationToken) == 0)
            {
                await transaction.RollbackAsync(cancellationToken);
                return false;
            }
        }

        foreach (var traceEvent in run.Events)
        {
            await using var command = connection.CreateCommand();
            command.Transaction = transaction;
            command.CommandText = "INSERT INTO event_index(run_id,sequence,event_id,kind,content_hash) VALUES($run,$sequence,$event,$kind,$hash)";
            command.Parameters.AddWithValue("$run", run.Id.ToString());
            command.Parameters.AddWithValue("$sequence", traceEvent.Sequence);
            command.Parameters.AddWithValue("$event", traceEvent.Id.ToString());
            command.Parameters.AddWithValue("$kind", traceEvent.Kind.ToString());
            command.Parameters.AddWithValue("$hash", traceEvent.ContentSha256);
            await command.ExecuteNonQueryAsync(cancellationToken);
        }

        await transaction.CommitAsync(cancellationToken);
        return true;
    }

    public async Task<TraceRun?> FindByImportAsync(
        string hash,
        string adapter,
        string version,
        CancellationToken cancellationToken)
    {
        await using var connection = Open();
        await using var command = connection.CreateCommand();
        command.CommandText = "SELECT data_json FROM runs WHERE input_hash=$hash AND adapter=$adapter AND adapter_version=$version";
        command.Parameters.AddWithValue("$hash", hash);
        command.Parameters.AddWithValue("$adapter", adapter);
        command.Parameters.AddWithValue("$version", version);
        return DeserializeRun(await command.ExecuteScalarAsync(cancellationToken));
    }

    public async Task<TraceRun?> GetAsync(Guid id, CancellationToken cancellationToken)
    {
        await using var connection = Open();
        await using var command = connection.CreateCommand();
        command.CommandText = "SELECT data_json FROM runs WHERE id=$id";
        command.Parameters.AddWithValue("$id", id.ToString());
        return DeserializeRun(await command.ExecuteScalarAsync(cancellationToken));
    }

    public async Task<IReadOnlyList<TraceRun>> ListAsync(CancellationToken cancellationToken)
    {
        var result = new List<TraceRun>();
        await using var connection = Open();
        await using var command = connection.CreateCommand();
        command.CommandText = "SELECT data_json FROM runs ORDER BY imported_at,id";
        await using var reader = await command.ExecuteReaderAsync(cancellationToken);
        while (await reader.ReadAsync(cancellationToken))
        {
            result.Add(JsonSerializer.Deserialize<TraceRun>(reader.GetString(0), JsonDefaults.Options)!);
        }

        return result;
    }

    async Task IAnalysisRepository.SaveAsync(
        AnalysisRevision revision,
        CancellationToken cancellationToken)
    {
        await using var connection = Open();
        await using var transaction = (SqliteTransaction)await connection.BeginTransactionAsync(cancellationToken);
        await using (var command = connection.CreateCommand())
        {
            command.Transaction = transaction;
            command.CommandText = "INSERT INTO analyses(id,run_id,created_at,data_json) VALUES($id,$run,$at,$json)";
            command.Parameters.AddWithValue("$id", revision.Id.ToString());
            command.Parameters.AddWithValue("$run", revision.RunId.ToString());
            command.Parameters.AddWithValue("$at", revision.CreatedAt.ToString("O"));
            command.Parameters.AddWithValue("$json", JsonSerializer.Serialize(revision, JsonDefaults.Options));
            await command.ExecuteNonQueryAsync(cancellationToken);
        }

        for (var index = 0; index < revision.Alerts.Count; index++)
        {
            var alert = revision.Alerts[index];
            await using var command = connection.CreateCommand();
            command.Transaction = transaction;
            command.CommandText = "INSERT INTO alert_index(analysis_id,ordinal,run_id,code,start_sequence,end_sequence) VALUES($analysis,$ordinal,$run,$code,$start,$end)";
            command.Parameters.AddWithValue("$analysis", revision.Id.ToString());
            command.Parameters.AddWithValue("$ordinal", index);
            command.Parameters.AddWithValue("$run", revision.RunId.ToString());
            command.Parameters.AddWithValue("$code", alert.Code);
            command.Parameters.AddWithValue("$start", alert.StartSequence);
            command.Parameters.AddWithValue("$end", alert.EndSequence);
            await command.ExecuteNonQueryAsync(cancellationToken);
        }

        await transaction.CommitAsync(cancellationToken);
    }

    public async Task<AnalysisRevision?> GetLatestAsync(
        Guid runId,
        CancellationToken cancellationToken)
    {
        await using var connection = Open();
        await using var command = connection.CreateCommand();
        command.CommandText = "SELECT data_json FROM analyses WHERE run_id=$id ORDER BY created_at DESC,rowid DESC LIMIT 1";
        command.Parameters.AddWithValue("$id", runId.ToString());
        var value = await command.ExecuteScalarAsync(cancellationToken);
        return value is string json
            ? JsonSerializer.Deserialize<AnalysisRevision>(json, JsonDefaults.Options)
            : null;
    }

    private void Initialize()
    {
        using var connection = Open();
        using var command = connection.CreateCommand();
        command.CommandText = "PRAGMA journal_mode=DELETE;";
        var journalMode = command.ExecuteScalar()?.ToString();
        if (!string.Equals(journalMode, "delete", StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException(
                $"SQLite refused required DELETE journal mode (reported {journalMode ?? "null"}).");
        }

        command.CommandText = """
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY,
                input_hash TEXT NOT NULL,
                adapter TEXT NOT NULL,
                adapter_version TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                data_json TEXT NOT NULL,
                UNIQUE(input_hash,adapter,adapter_version));
            CREATE TABLE IF NOT EXISTS event_index(
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                PRIMARY KEY(run_id,sequence),
                FOREIGN KEY(run_id) REFERENCES runs(id));
            CREATE INDEX IF NOT EXISTS ix_event_run_sequence ON event_index(run_id,sequence);
            CREATE TABLE IF NOT EXISTS analyses(
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                data_json TEXT NOT NULL,
                FOREIGN KEY(run_id) REFERENCES runs(id));
            CREATE INDEX IF NOT EXISTS ix_analyses_run_created ON analyses(run_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS alert_index(
                analysis_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                run_id TEXT NOT NULL,
                code TEXT NOT NULL,
                start_sequence INTEGER NOT NULL,
                end_sequence INTEGER NOT NULL,
                PRIMARY KEY(analysis_id,ordinal),
                FOREIGN KEY(analysis_id) REFERENCES analyses(id),
                FOREIGN KEY(run_id) REFERENCES runs(id));
            CREATE INDEX IF NOT EXISTS ix_alert_run_code ON alert_index(run_id,code);
            """;
        command.ExecuteNonQuery();
    }

    private SqliteConnection Open()
    {
        EnsurePrivateDatabaseFile(databasePath);
        var connection = new SqliteConnection(connectionString);
        connection.Open();
        using var pragma = connection.CreateCommand();
        pragma.CommandText = "PRAGMA foreign_keys=ON;";
        pragma.ExecuteNonQuery();
        return connection;
    }

    private static TraceRun? DeserializeRun(object? value) => value is string json
        ? JsonSerializer.Deserialize<TraceRun>(json, JsonDefaults.Options)
        : null;
}
