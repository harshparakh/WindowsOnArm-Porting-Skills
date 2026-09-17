using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.Json;

namespace RepoToArm.Workbench;

public enum BackendAction { Scout, Status, Resume, Approve, Port, Build, Verify, InjectFault, Repair, Export }

public sealed class BackendClient
{
    // Only this method translates operator actions into the backend CLI contract.
    public static ProcessStartInfo CreateStartInfo(
        WorkbenchSettings settings, BackendAction action, string target, string? planHash = null)
    {
        var configurationError = settings.Validate();
        if (configurationError is not null)
            throw new ArgumentException(configurationError);
        if (action == BackendAction.Scout ? !InputValidation.IsRepository(target) : !InputValidation.IsRunId(target))
            throw new ArgumentException("The repository or run ID is invalid.");
        if (action == BackendAction.Approve && (string.IsNullOrWhiteSpace(planHash) || planHash.StartsWith('-') ||
            planHash.Any(char.IsWhiteSpace)))
            throw new ArgumentException("Approval requires the current plan hash.");
        if (action == BackendAction.Scout &&
            InputValidation.SourceOptionsError(settings.LastSourceCommit, settings.LastProject, settings.LastScope) is { } sourceError)
            throw new ArgumentException(sourceError);

        var info = new ProcessStartInfo
        {
            FileName = settings.PythonPath,
            WorkingDirectory = settings.BackendRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8
        };
        var command = action switch
        {
            BackendAction.Scout => "scout",
            BackendAction.Status => "status",
            BackendAction.Resume => "resume",
            BackendAction.Approve => "approve",
            BackendAction.Port => "port",
            BackendAction.Build => "build",
            BackendAction.Verify => "verify",
            BackendAction.InjectFault => "inject-fault",
            BackendAction.Repair => "repair",
            BackendAction.Export => "export",
            _ => throw new ArgumentOutOfRangeException(nameof(action))
        };
        foreach (var argument in new[] { "-m", "workbench.cli", "--root", settings.RunsRoot, command, target })
            info.ArgumentList.Add(argument);
        if (action == BackendAction.Approve)
        {
            info.ArgumentList.Add("--plan-hash");
            info.ArgumentList.Add(planHash!);
        }
        if (action == BackendAction.Scout)
        {
            foreach (var (name, value) in new[] { ("--commit", settings.LastSourceCommit),
                ("--project", settings.LastProject), ("--scope", settings.LastScope) })
            {
                if (!string.IsNullOrWhiteSpace(value))
                {
                    info.ArgumentList.Add(name);
                    info.ArgumentList.Add(value);
                }
            }
        }
        info.Environment["PYTHONUNBUFFERED"] = "1";
        info.Environment["PYTHONIOENCODING"] = "utf-8";
        return info;
    }

    public async Task<BackendOutcome> ExecuteAsync(
        WorkbenchSettings settings, BackendAction action, string target, string? planHash,
        IProgress<BackendNotice> progress, CancellationToken cancellationToken)
    {
        using var process = new Process { StartInfo = CreateStartInfo(settings, action, target, planHash) };
        cancellationToken.ThrowIfCancellationRequested();
        if (!process.Start())
            throw new InvalidOperationException("The configured Python process did not start.");

        RunState? run = null;
        var hasFinalResult = false;
        var hasTerminalRecord = false;
        string? protocolError = null;
        using var streamCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        var stdout = ObserveStreamAsync(ReadOutputAsync);
        var stderr = ObserveStreamAsync(ReadErrorAsync);
        try
        {
            await Task.WhenAll(stdout, stderr, process.WaitForExitAsync(streamCancellation.Token)).ConfigureAwait(false);
        }
        catch (Exception error) when (error is OperationCanceledException or IOException)
        {
            try
            {
                if (!process.HasExited)
                    process.Kill(entireProcessTree: true);
                using var stopTimeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                await process.WaitForExitAsync(stopTimeout.Token).ConfigureAwait(false);
            }
            catch (Exception stopError) when (stopError is Win32Exception or InvalidOperationException or OperationCanceledException)
            {
                progress.Report(new BackendNotice("UI", $"Could not confirm the owned process stopped: {stopError.Message}", true));
            }
            throw;
        }

        return new BackendOutcome(process.ExitCode, run, hasFinalResult, protocolError);

        async Task ObserveStreamAsync(Func<Task> read)
        {
            try
            {
                await read().ConfigureAwait(false);
            }
            catch (IOException)
            {
                streamCancellation.Cancel();
                throw;
            }
        }

        async Task ReadOutputAsync()
        {
            while (await process.StandardOutput.ReadLineAsync(streamCancellation.Token).ConfigureAwait(false) is { } line)
            {
                if (string.IsNullOrWhiteSpace(line))
                    continue;
                try
                {
                    using var document = JsonDocument.Parse(line);
                    var root = document.RootElement;
                    if (root.ValueKind != JsonValueKind.Object)
                        throw new JsonException("Expected a JSON object.");
                    if (hasTerminalRecord)
                        throw new JsonException("A record arrived after the terminal result or error.");
                    var kind = Text(root, "kind");
                    if (kind is "result" or "error")
                    {
                        if (root.TryGetProperty("run", out var state) && state.ValueKind == JsonValueKind.Object)
                        {
                            var received = state.Deserialize<RunState>(SettingsStore.JsonOptions);
                            if (received is null || !InputValidation.IsRunId(received.Id))
                                throw new JsonException("Run result has no valid ID.");
                            if (action != BackendAction.Scout && received.Id != target)
                                throw new JsonException("Run result does not match the selected run.");
                            run = received;
                        }
                        if (kind == "result")
                        {
                            if (!root.TryGetProperty("run", out var finalRun) ||
                                finalRun.ValueKind != JsonValueKind.Object || run is null)
                                throw new JsonException("Expected exactly one final run result.");
                            hasFinalResult = true;
                        }
                        else
                        {
                            protocolError = Text(root, "error") ?? "Backend reported an error.";
                            progress.Report(new BackendNotice("error", protocolError, true));
                        }
                        hasTerminalRecord = true;
                    }
                    else if (kind == "event")
                    {
                        progress.Report(new BackendNotice(Text(root, "stage") ?? "event",
                            Text(root, "message") ?? line, false, Text(root, "runId"), Text(root, "status")));
                    }
                    else
                    {
                        protocolError ??= "Unexpected JSONL record. Refresh status before another action.";
                        progress.Report(new BackendNotice("protocol", line, true));
                    }
                }
                catch (JsonException error)
                {
                    protocolError = $"Invalid backend JSONL: {error.Message}";
                    progress.Report(new BackendNotice("protocol", $"{protocolError}\n{line}", true));
                }
            }
        }

        async Task ReadErrorAsync()
        {
            while (await process.StandardError.ReadLineAsync(streamCancellation.Token).ConfigureAwait(false) is { } line)
                progress.Report(new BackendNotice("stderr", line, true));
        }
    }

    private static string? Text(JsonElement element, string name) =>
        element.TryGetProperty(name, out var property) && property.ValueKind == JsonValueKind.String
            ? property.GetString() : null;
}
