using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace RepoToArm.Workbench;

public sealed record WorkbenchSettings
{
    public string PythonPath { get; init; } = "";
    public string BackendRoot { get; init; } = "";
    public string RunsRoot { get; init; } = System.IO.Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AgencyCowork", "runs", "RepoToArm");
    public string LastRunId { get; init; } = "";
    public string LastRepository { get; init; } = "";
    public string LastSourceCommit { get; init; } = "";
    public string LastProject { get; init; } = "";
    public string LastScope { get; init; } = "";

    public static WorkbenchSettings ApplyArguments(WorkbenchSettings settings, string[] arguments)
    {
        var original = settings;
        for (var index = 0; index < arguments.Length; index += 2)
        {
            if (index + 1 >= arguments.Length || arguments[index + 1].StartsWith("--", StringComparison.Ordinal))
                throw new ArgumentException("Each option requires an explicit value.");
            var value = arguments[index + 1];
            settings = arguments[index] switch
            {
                "--python" => settings with { PythonPath = value },
                "--backend-root" => settings with { BackendRoot = value },
                "--runs-root" => settings with { RunsRoot = value },
                "--repo" => settings with { LastRepository = value },
                "--commit" => settings with { LastSourceCommit = value },
                "--project" => settings with { LastProject = value },
                "--scope" => settings with { LastScope = value },
                _ => throw new ArgumentException($"Unknown option: {arguments[index]}. Supported: --python, --backend-root, --runs-root, --repo, --commit, --project, --scope.")
            };
        }
        if (!string.Equals(original.BackendRoot, settings.BackendRoot, StringComparison.OrdinalIgnoreCase) ||
            !string.Equals(original.RunsRoot, settings.RunsRoot, StringComparison.OrdinalIgnoreCase))
            settings = settings with { LastRunId = "" };
        return settings;
    }

    public string? Validate()
    {
        foreach (var (path, label) in new[] { (PythonPath, "Python executable"), (BackendRoot, "Backend root"), (RunsRoot, "Runs root") })
        {
            var error = LocalPaths.Validate(path);
            if (error is not null)
                return $"{label}: {error}";
        }
        if (!File.Exists(PythonPath) || !System.IO.Path.GetExtension(PythonPath).Equals(".exe", StringComparison.OrdinalIgnoreCase))
            return "Choose an existing Python .exe, without arguments.";
        if (!Directory.Exists(BackendRoot) || !File.Exists(System.IO.Path.Combine(BackendRoot, "workbench", "cli.py")))
            return "Backend root must contain workbench\\cli.py.";
        if (File.Exists(RunsRoot))
            return "Runs root must be a directory, not a file.";
        return null;
    }
}

public sealed class SettingsStore
{
    internal static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        PropertyNameCaseInsensitive = true,
        WriteIndented = true
    };

    public string FilePath { get; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "AgencyCowork", "RepoToArm", "ui-settings.json");

    public WorkbenchSettings Load() => File.Exists(FilePath)
        ? JsonSerializer.Deserialize<WorkbenchSettings>(File.ReadAllText(FilePath), JsonOptions)
            ?? throw new JsonException("Expected a settings object.")
        : new WorkbenchSettings();

    public async Task SaveAsync(WorkbenchSettings settings)
    {
        var root = File.Exists(FilePath)
            ? JsonNode.Parse(await File.ReadAllTextAsync(FilePath)) as JsonObject
                ?? throw new JsonException("Existing configuration is not an object; refusing to overwrite it.")
            : new JsonObject();
        var updates = JsonSerializer.SerializeToNode(settings, JsonOptions)?.AsObject()
            ?? throw new JsonException("Could not serialize configuration.");
        foreach (var entry in updates)
            root[entry.Key] = entry.Value?.DeepClone();

        Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
        var staging = FilePath + "." + Guid.NewGuid().ToString("N") + ".new";
        try
        {
            await using (var stream = new FileStream(staging, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                4096, FileOptions.Asynchronous | FileOptions.WriteThrough))
            {
                await JsonSerializer.SerializeAsync(stream, root, JsonOptions);
                await stream.FlushAsync();
                stream.Flush(flushToDisk: true);
            }
            File.Move(staging, FilePath, overwrite: true);
        }
        finally
        {
            if (File.Exists(staging))
                File.Delete(staging);
        }
    }
}

public static class LocalPaths
{
    public static string? Validate(string? value)
    {
        if (string.IsNullOrWhiteSpace(value) || !Path.IsPathFullyQualified(value) ||
            value.StartsWith(@"\\", StringComparison.Ordinal) || value.StartsWith("//", StringComparison.Ordinal))
            return "Enter an absolute local path outside OneDrive.";
        try
        {
            var fullPath = Path.GetFullPath(value);
            if (fullPath.Split(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                .Any(part => part.StartsWith("OneDrive", StringComparison.OrdinalIgnoreCase)))
                return "OneDrive paths are not permitted.";
            foreach (var name in new[] { "OneDrive", "OneDriveCommercial", "OneDriveConsumer" })
            {
                var cloud = Environment.GetEnvironmentVariable(name);
                if (!string.IsNullOrWhiteSpace(cloud) && IsWithin(fullPath, cloud))
                    return "OneDrive paths are not permitted.";
            }
            for (var current = fullPath; current is not null; current = Path.GetDirectoryName(current))
            {
                if ((File.Exists(current) || Directory.Exists(current)) &&
                    (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                    return "Use a direct local path, not a symbolic link or junction.";
            }
            return null;
        }
        catch (Exception error) when (error is ArgumentException or IOException or UnauthorizedAccessException or NotSupportedException)
        {
            return error.Message;
        }
    }

    public static bool IsWithin(string path, string root)
    {
        var fullPath = Path.GetFullPath(path);
        var fullRoot = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        return fullPath.Equals(fullRoot, StringComparison.OrdinalIgnoreCase) ||
            fullPath.StartsWith(fullRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
    }
}
