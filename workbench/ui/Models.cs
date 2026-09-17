using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Windows.Input;

namespace RepoToArm.Workbench;

public sealed record RunState
{
    public string? Id { get; init; }
    public string? Repository { get; init; }
    public string? SourceCommit { get; init; }
    public string? Stage { get; init; }
    public string? Status { get; init; }
    public string? PlanHash { get; init; }
    public string? Project { get; init; }
    public string? ApprovalKind { get; init; }
    public JsonElement? ApprovalPlan { get; init; }
    public List<RunStage>? Stages { get; init; }
    public Dictionary<string, JsonElement>? Artifacts { get; init; }
    public RunTotals? Totals { get; init; }
    public InterruptedPortState? InterruptedPort { get; init; }
    public bool HasSupportedApprovalKind => ApprovalKind is "source-plan" or "build-patch" or "interrupted-port";

    public string? Artifact(string key) =>
        Artifacts?.TryGetValue(key, out var value) == true && value.ValueKind == JsonValueKind.String
            ? value.GetString() : null;

    public bool StageHasStatus(string id, params string[] statuses) =>
        Stages?.Any(stage => stage.Id == id &&
            statuses.Contains(stage.Status, StringComparer.OrdinalIgnoreCase)) == true;
}

public sealed record RunStage
{
    public string? Id { get; init; }
    public string? Title { get; init; }
    public string? Status { get; init; }
    public string? Detail { get; init; }
}

public sealed record RunTotals
{
    public long? Files { get; init; }
    public long? Invalids { get; init; }
}

public sealed record InterruptedPortState
{
    public string? Reason { get; init; }
}

public sealed record StageRow(string Id, string Number, string Title, string Status, string Detail)
{
    public string DisplayStatus => Presentation.Status(Status);
    public string Accent => Presentation.Accent(Status);
}

public sealed record ArtifactRow(string Title, string Path);
public sealed record LogRow(string Time, string Source, string Message, string Color);
public sealed record BackendNotice(string Source, string Message, bool IsError = false, string? RunId = null, string? Status = null);
public sealed record BackendOutcome(int ExitCode, RunState? Run, bool HasFinalResult, string? ProtocolError);

public static partial class InputValidation
{
    [GeneratedRegex(@"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$", RegexOptions.CultureInvariant)]
    private static partial Regex RepositoryPattern();

    [GeneratedRegex(@"^[0-9a-f]{12}$", RegexOptions.CultureInvariant)]
    private static partial Regex RunIdPattern();

    public static bool IsRunId(string? value) => value is not null && RunIdPattern().IsMatch(value);

    public static string? SourceOptionsError(string commit, string project, string scope)
    {
        if (commit.Length != 0 && (commit.Length != 40 || commit.Any(character => !Uri.IsHexDigit(character) || char.IsUpper(character))))
            return "Use a full lowercase 40-character commit, or leave it blank to resolve the default branch.";
        if (project.Length != 0 && (System.IO.Path.IsPathRooted(project) || project.Contains(':') ||
            project.Replace('\\', '/').Split('/').Any(part => part is "" or "." or "..") ||
            !(project.EndsWith(".csproj", StringComparison.OrdinalIgnoreCase) || project.EndsWith(".vbproj", StringComparison.OrdinalIgnoreCase))))
            return "Use a repository-relative SDK project path ending in .csproj or .vbproj.";
        return scope.Length > 4000 ? "Keep the port scope within 4,000 characters." : null;
    }

    public static bool IsRepository(string value)
    {
        if (RepositoryPattern().IsMatch(value))
            return true;
        return Uri.TryCreate(value, UriKind.Absolute, out var uri) &&
            uri.Scheme == Uri.UriSchemeHttps && uri.Host.Equals("github.com", StringComparison.OrdinalIgnoreCase) &&
            uri.IsDefaultPort && string.IsNullOrEmpty(uri.UserInfo) && string.IsNullOrEmpty(uri.Query) &&
            string.IsNullOrEmpty(uri.Fragment) && RepositoryPattern().IsMatch(uri.AbsolutePath.Trim('/'));
    }
}

public static class Presentation
{
    public static string Known(string? value) => string.IsNullOrWhiteSpace(value) ? "Not reported" : value;
    public static string Status(string? value) => string.IsNullOrWhiteSpace(value)
        ? "UNKNOWN" : value.Replace('-', ' ').Replace('_', ' ').ToUpperInvariant();

    public static string Accent(string? status) => status?.ToLowerInvariant() switch
    {
        "approved" or "passed" or "verified" or "device-verified" => "#116344",
        "blocked" or "needs-approval" or "needs-verification" or "partial" or "canceled" or "interrupted" => "#925410",
        "failed" or "error" => "#B3261E",
        "running" => "#185ABD",
        _ => "#526273"
    };
}

public abstract class ObservableObject : INotifyPropertyChanged
{
    public event PropertyChangedEventHandler? PropertyChanged;
    protected void Changed([CallerMemberName] string? name = null) =>
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}

public sealed class RelayCommand(Action execute, Func<bool>? canExecute = null) : ICommand
{
    public bool CanExecute(object? parameter) => canExecute?.Invoke() ?? true;
    public void Execute(object? parameter)
    {
        if (CanExecute(parameter))
            execute();
    }
    public event EventHandler? CanExecuteChanged;
    public void Refresh() => CanExecuteChanged?.Invoke(this, EventArgs.Empty);
}

public sealed class AsyncCommand(Func<Task> execute, Func<bool> canExecute) : ICommand
{
    public bool CanExecute(object? parameter) => canExecute();
    public async void Execute(object? parameter)
    {
        if (CanExecute(parameter))
            await execute();
    }
    public event EventHandler? CanExecuteChanged;
    public void Refresh() => CanExecuteChanged?.Invoke(this, EventArgs.Empty);
}
