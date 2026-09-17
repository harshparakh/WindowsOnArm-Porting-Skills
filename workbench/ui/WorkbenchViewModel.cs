using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;

namespace RepoToArm.Workbench;

public sealed class WorkbenchViewModel : ObservableObject
{
    private const int LogLimit = 1000;
    private static readonly TimeZoneInfo EasternTime = TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time");
    private readonly SettingsStore _store;
    private readonly BackendClient _backend = new();
    private readonly string? _startupWarning;
    private WorkbenchSettings _settings;
    private RunState? _run;
    private bool _runFresh;
    private bool _configurationDirty;
    private bool _busy;
    private bool _cancelling;
    private CancellationTokenSource? _cancellation;
    private TaskCompletionSource? _completion;
    private string _repository;
    private string _sourceCommit;
    private string _sourceProject;
    private string _sourceScope;
    private string _runId;
    private string _pythonPath;
    private string _backendRoot;
    private string _runsRoot;
    private string _notice;
    private string _noticeColor = "#526273";
    private string? _operationIssue;
    private string _activeOperation = "";
    private bool _configurationOpen;
    private int _discardedLines;

    public WorkbenchViewModel(WorkbenchSettings settings, SettingsStore store, string? warning)
    {
        _settings = settings;
        _store = store;
        _startupWarning = warning;
        _pythonPath = settings.PythonPath ?? "";
        _backendRoot = settings.BackendRoot ?? "";
        _runsRoot = settings.RunsRoot ?? "";
        _repository = settings.LastRepository ?? "";
        _sourceCommit = settings.LastSourceCommit ?? "";
        _sourceProject = settings.LastProject ?? "";
        _sourceScope = settings.LastScope ?? "";
        _runId = settings.LastRunId ?? "";
        _notice = warning ?? "Configure the backend, then analyze a repository. No candidate commands run in this UI.";
        _configurationOpen = warning is not null || CurrentSettings.Validate() is not null;

        AnalyzeCommand = new AsyncCommand(() => ExecuteAsync(BackendAction.Scout), () =>
            Ready && InputValidation.IsRepository(Repository.Trim()) && SourceOptionsError is null);
        RefreshCommand = new AsyncCommand(() => ExecuteAsync(BackendAction.Status), () => Ready && InputValidation.IsRunId(RunId.Trim()));
        ResumeCommand = new AsyncCommand(() => ExecuteAsync(BackendAction.Resume), () =>
            Ready && _runFresh && _run?.Id == RunId.Trim() &&
            _run?.Status?.Equals("running", StringComparison.OrdinalIgnoreCase) == true);
        ApproveCommand = new AsyncCommand(ApproveAsync, () => Mutable && HasPlan && NeedsApproval);
        PortCommand = new AsyncCommand(() => ExecuteAsync(BackendAction.Port), () =>
            Mutable && HasPlan && Approved &&
            (HasInterruptedPort
                ? _run?.ApprovalKind == "interrupted-port"
                : _run?.ApprovalKind == "source-plan" ||
                    (_run?.ApprovalKind == "build-patch" && _run.StageHasStatus("build", "failed"))));
        BuildCommand = new AsyncCommand(() => ExecuteAsync(BackendAction.Build), () =>
            Mutable && HasPlan && Approved && !HasInterruptedPort && _run?.ApprovalKind == "build-patch");
        VerifyCommand = new AsyncCommand(() => ExecuteAsync(BackendAction.Verify), () =>
            Mutable && HasPackage && !NeedsApproval && !HasInterruptedPort);
        InjectFaultCommand = new AsyncCommand(InjectFaultAsync, () =>
            Mutable && HasPackage && !NeedsApproval && !HasInterruptedPort &&
            _run?.StageHasStatus("verify", "passed", "verified") == true);
        RepairCommand = new AsyncCommand(() => ExecuteAsync(BackendAction.Repair), () =>
            Mutable && HasPackage && !NeedsApproval && !HasInterruptedPort && _run?.StageHasStatus("verify", "failed") == true);
        ExportCommand = new AsyncCommand(() => ExecuteAsync(BackendAction.Export), () =>
            Mutable && !HasInterruptedPort &&
            (_run?.Artifacts?.Any(pair => pair.Key != "root" && pair.Value.ValueKind == JsonValueKind.String) == true));
        SaveSettingsCommand = new AsyncCommand(SaveSettingsAsync, () => !IsBusy && CurrentSettings.Validate() is null);
        CancelCommand = new RelayCommand(Cancel, () => IsCommandActive && !_cancelling);
        OpenOutputCommand = new RelayCommand(OpenOutput, () => !IsBusy && OutputPath() is not null);
        PopulateState();
    }

    public ObservableCollection<StageRow> Stages { get; } = [];
    public ObservableCollection<ArtifactRow> Artifacts { get; } = [];
    public ObservableCollection<LogRow> Logs { get; } = [];
    public AsyncCommand AnalyzeCommand { get; }
    public AsyncCommand RefreshCommand { get; }
    public AsyncCommand ResumeCommand { get; }
    public AsyncCommand ApproveCommand { get; }
    public AsyncCommand PortCommand { get; }
    public AsyncCommand BuildCommand { get; }
    public AsyncCommand VerifyCommand { get; }
    public AsyncCommand InjectFaultCommand { get; }
    public AsyncCommand RepairCommand { get; }
    public AsyncCommand ExportCommand { get; }
    public AsyncCommand SaveSettingsCommand { get; }
    public RelayCommand CancelCommand { get; }
    public RelayCommand OpenOutputCommand { get; }

    public bool IsBusy => _busy;
    public bool IsIdle => !_busy;
    public bool IsCommandActive => _cancellation is not null;
    public bool HasRun => _run is not null;
    public string ConfigFile => _store.FilePath;
    public string Notice => _notice;
    public string NoticeColor => _noticeColor;
    public string ActiveOperation => _activeOperation;
    public string StateStatus => Presentation.Status(_run?.Status);
    public string StateColor => Presentation.Accent(_run?.Status);
    public string BannerStatus => Presentation.Status(_operationIssue ?? _run?.Status);
    public string BannerColor => Presentation.Accent(_operationIssue ?? _run?.Status);
    public string BannerLabel => _operationIssue is null ? "LAST REPORTED STATE" : "LAST COMMAND";
    public string CurrentStage => Presentation.Known(_run?.Stage);
    public string SelectedRun => Presentation.Known(_run?.Id);
    public string SelectedRepository => Presentation.Known(_run?.Repository);
    public string Commit => Presentation.Known(_run?.SourceCommit);
    public string PlanHash => Presentation.Known(_run?.PlanHash);
    public string Project => Presentation.Known(_run?.Project);
    public string ApprovalKind => Presentation.Known(_run?.ApprovalKind);
    public string ExactPlan => _run?.ApprovalPlan is { ValueKind: JsonValueKind.Object } plan
        ? JsonSerializer.Serialize(plan, SettingsStore.JsonOptions) : "No approval plan has been reported.";
    public string? SourceOptionsError => InputValidation.SourceOptionsError(SourceCommitInput.Trim(), SourceProject.Trim(), SourceScope.Trim());
    public string SourceOptionsHelp => SourceOptionsError ?? "Optional pinned commit, SDK project and scope. Blank commit resolves the current default branch.";
    public string ApprovalButtonLabel => _run?.HasSupportedApprovalKind == true
        ? $"Approve {_run.ApprovalKind}" : "Approve current plan";
    public bool HasInterruptedPort => _run?.ApprovalKind == "interrupted-port" ||
        _run?.InterruptedPort is not null || _run?.StageHasStatus("port", "interrupted") == true;
    public string InterruptionReason => Presentation.Known(_run?.InterruptedPort?.Reason);
    public string PortButtonLabel => HasInterruptedPort ? "Resume port with agent" : "Port with agent";
    public string PlanPatchLabel => HasInterruptedPort ? "PARTIAL SOURCE DIFF" : "SOURCE DIFF";
    public string PlanPatch => Presentation.Known(_run?.Artifact(HasInterruptedPort ? "partialPatch" : "patch"));
    public string Totals => $"Files: {_run?.Totals?.Files?.ToString("N0") ?? "unknown"}    Invalids: {_run?.Totals?.Invalids?.ToString("N0") ?? "unknown"}";
    public string StateFootnote => _run is null ? "No run loaded." :
        IsCommandActive ? "A command is active. These fields are from the last backend response." :
        !_runFresh ? "Last reported state. Refresh before taking another action." :
        "Reported by the backend. Package checks are separate from native device evidence.";
    public string ApprovalHelp => HasInterruptedPort
        ? NeedsApproval
            ? "Partial edits are not a completed port. Review the partial diff and approve this hash to permit resume."
            : "Partial edits remain incomplete. Port resumes the approved plan; build and verification stay disabled."
        : !NeedsApproval
        ? "Approval is never automatic. The backend decides when a new plan requires approval."
        : _run?.ApprovalKind == "build-patch"
            ? "Review the source diff and isolated build plan in the run folder before approving this exact hash."
            : "Review the pinned source plan. Port validates the unchanged baseline on an isolated runner before invoking the restricted agent.";
    public string RepositoryHelp => string.IsNullOrWhiteSpace(Repository) || InputValidation.IsRepository(Repository.Trim())
        ? "Public GitHub URL or owner/repo. Do not enter credentials."
        : "Use https://github.com/owner/repo or owner/repo, without credentials, query strings or fragments.";
    public string LogSummary => _discardedLines > 0
        ? $"Latest {LogLimit:N0} lines. {_discardedLines:N0} older UI lines discarded."
        : "Backend events, stderr and labeled UI errors. Receipt times are ET.";
    public string ConfigurationMessage => CurrentSettings.Validate() ??
        (_configurationDirty ? "Save configuration before running a command." : "Local paths validated. Runner and agent permissions are configured in the backend.");
    public string ActionHelp => IsCommandActive ? "A backend command is active. Its returned state determines the next step." :
        !Ready ? "Save a valid configuration to enable actions." :
        _run is null ? "Analyze a repository or load a run ID to begin." :
        !_runFresh ? "Refresh this run to replace stale state before continuing." :
        NeedsApproval ? "The current plan is awaiting your approval." :
        HasInterruptedPort ? "Resume the explicitly approved partial edits. No completed port is assumed." :
        _run.Status?.Equals("running", StringComparison.OrdinalIgnoreCase) == true
            ? "The backend still reports running. Resume/reconcile acquires its lock before recovering interrupted work or monitoring an existing job."
            : "Only actions supported by the reported state are enabled. The backend enforces approval and isolation.";

    public bool ConfigurationOpen
    {
        get => _configurationOpen;
        set { _configurationOpen = value; Changed(); }
    }
    public string Repository
    {
        get => _repository;
        set { _repository = value; RefreshBindings(); }
    }
    public string SourceCommitInput
    {
        get => _sourceCommit;
        set { _sourceCommit = value; _runFresh = false; RefreshBindings(); }
    }
    public string SourceProject
    {
        get => _sourceProject;
        set { _sourceProject = value; _runFresh = false; RefreshBindings(); }
    }
    public string SourceScope
    {
        get => _sourceScope;
        set { _sourceScope = value; _runFresh = false; RefreshBindings(); }
    }
    public string RunId
    {
        get => _runId;
        set
        {
            _runId = value;
            if (_run?.Id != value.Trim())
                _runFresh = false;
            RefreshBindings();
        }
    }
    public string PythonPath
    {
        get => _pythonPath;
        set { _pythonPath = value; ConfigurationEdited(); }
    }
    public string BackendRoot
    {
        get => _backendRoot;
        set { _backendRoot = value; ConfigurationEdited(); }
    }
    public string RunsRoot
    {
        get => _runsRoot;
        set { _runsRoot = value; ConfigurationEdited(); }
    }

    private WorkbenchSettings CurrentSettings => _settings with
    {
        PythonPath = PythonPath.Trim(), BackendRoot = BackendRoot.Trim(), RunsRoot = RunsRoot.Trim(),
        LastRunId = RunId.Trim(), LastRepository = Repository.Trim(),
        LastSourceCommit = SourceCommitInput.Trim(), LastProject = SourceProject.Trim(), LastScope = SourceScope.Trim()
    };
    private bool Ready => !IsBusy && !_configurationDirty && _settings.Validate() is null;
    private bool Mutable => Ready && _runFresh && _run is not null && _run.Id == RunId.Trim() &&
        _run.Status is { } status && !status.Equals("running", StringComparison.OrdinalIgnoreCase);
    private bool HasPlan => _run?.HasSupportedApprovalKind == true &&
        !string.IsNullOrWhiteSpace(_run.PlanHash) &&
        !string.IsNullOrWhiteSpace(_run?.SourceCommit) && !string.IsNullOrWhiteSpace(_run?.Project);
    private bool NeedsApproval => _run?.StageHasStatus("approval", "needs-approval") == true ||
        _run?.Status?.Equals("needs-approval", StringComparison.OrdinalIgnoreCase) == true;
    private bool Approved => !NeedsApproval && _run?.StageHasStatus("approval", "approved") == true;
    private bool HasPackage => !string.IsNullOrWhiteSpace(_run?.Artifact("arm64Package"));

    public async Task RestoreAsync()
    {
        if (_startupWarning is null && RefreshCommand.CanExecute(null))
            await ExecuteAsync(BackendAction.Status);
    }

    public async Task StopAsync()
    {
        Cancel();
        if (_completion is not null)
            await _completion.Task;
    }

    private async Task ApproveAsync()
    {
        var text = $"Approve this exact backend plan?\n\nRepository: {SelectedRepository}\nCommit: {Commit}\n" +
            $"Project: {Project}\nPlan hash: {PlanHash}\nApproval kind: {ApprovalKind}\n\n" +
            "This records approval only. It does not start porting or building.";
        if (MessageBox.Show(text, "Approve current plan", MessageBoxButton.YesNo, MessageBoxImage.Question,
            MessageBoxResult.No) == MessageBoxResult.Yes)
            await ExecuteAsync(BackendAction.Approve);
    }

    private async Task InjectFaultAsync()
    {
        if (MessageBox.Show($"Request a deliberate demo fault for run {SelectedRun}?\n\n" +
            "This invokes the backend fault-injection action on the run's package. It does not simulate a failed status.",
            "Inject demo fault", MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No) == MessageBoxResult.Yes)
            await ExecuteAsync(BackendAction.InjectFault);
    }

    private async Task ExecuteAsync(BackendAction action)
    {
        if (IsBusy)
            return;
        var target = action == BackendAction.Scout ? Repository.Trim() : RunId.Trim();
        var hash = action == BackendAction.Approve ? _run?.PlanHash : null;
        _busy = true;
        _cancelling = false;
        _runFresh = false;
        _operationIssue = null;
        _completion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        _cancellation = new CancellationTokenSource();
        _activeOperation = $"Executing backend command: {action}. Completion has not been reported.";
        SetNotice("The owned CLI is active. Live output appears below.", "#185ABD");
        RefreshBindings();
        try
        {
            _settings = CurrentSettings;
            await _store.SaveAsync(_settings);
            if (action == BackendAction.Scout)
            {
                _run = null;
                _runFresh = false;
                _runId = "";
                _settings = _settings with { LastRunId = "" };
                await _store.SaveAsync(_settings);
                PopulateState();
            }
            var progress = new Progress<BackendNotice>(notice =>
            {
                AppendLog(notice);
                if (action == BackendAction.Scout && InputValidation.IsRunId(notice.RunId))
                {
                    _runId = notice.RunId!;
                    Changed(nameof(RunId));
                }
                if (IsCommandActive && notice.Status is not null && notice.RunId == _runId)
                {
                    var index = Stages.ToList().FindIndex(stage => stage.Id == notice.Source);
                    if (index >= 0)
                        Stages[index] = Stages[index] with { Status = notice.Status, Detail = notice.Message };
                }
            });
            var outcome = await _backend.ExecuteAsync(_settings, action, target, hash, progress, _cancellation.Token);
            if (outcome.Run is not null)
            {
                _run = outcome.Run;
                _runId = outcome.Run.Id!;
                _repository = outcome.Run.Repository ?? _repository;
            }
            _runFresh = outcome.ExitCode == 0 && outcome.HasFinalResult && outcome.ProtocolError is null;
            if (!_runFresh)
            {
                _operationIssue = "failed";
                SetNotice($"FAILED. Backend exit code: {outcome.ExitCode}. " +
                    (outcome.ProtocolError ?? (outcome.HasFinalResult ? "See stderr below." : "No final run result was received.")) +
                    " Refresh status before continuing.", "#B3261E");
            }
            else
            {
                SetNotice($"Backend returned {StateStatus} for {CurrentStage}. " +
                    (_run?.Status?.Equals("running", StringComparison.OrdinalIgnoreCase) == true
                        ? "No completion is claimed. Refresh status to check." : "Review the reported state and evidence below."),
                    StateColor);
            }
        }
        catch (OperationCanceledException)
        {
            _runFresh = false;
            _operationIssue = "canceled";
            SetNotice("CANCELED. Stopping the owned CLI does not confirm remote jobs stopped. Refresh status before continuing.", "#925410");
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or
            Win32Exception or InvalidOperationException or ArgumentException or NotSupportedException)
        {
            _runFresh = false;
            _operationIssue = "failed";
            AppendLog(new BackendNotice("UI", error.Message, true));
            SetNotice($"FAILED. {error.Message}", "#B3261E");
        }
        finally
        {
            try
            {
                _settings = CurrentSettings;
                await _store.SaveAsync(_settings);
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException)
            {
                AppendLog(new BackendNotice("UI", $"Run selection could not be saved: {error.Message}", true));
                SetNotice($"{Notice} Run selection could not be saved.", "#B3261E");
            }
            _busy = false;
            _activeOperation = "";
            _cancellation.Dispose();
            _cancellation = null;
            PopulateState();
            _completion.TrySetResult();
        }
    }

    private async Task SaveSettingsAsync()
    {
        if (IsBusy)
            return;
        _busy = true;
        _completion = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        RefreshBindings();
        try
        {
            var proposed = CurrentSettings;
            var error = proposed.Validate();
            if (error is not null)
                throw new ArgumentException(error);
            if (!string.Equals(_settings.BackendRoot, proposed.BackendRoot, StringComparison.OrdinalIgnoreCase) ||
                !string.Equals(_settings.RunsRoot, proposed.RunsRoot, StringComparison.OrdinalIgnoreCase))
            {
                proposed = proposed with { LastRunId = "" };
                _run = null;
                _runId = "";
                _runFresh = false;
            }
            await _store.SaveAsync(proposed);
            _settings = proposed;
            _configurationDirty = false;
            SetNotice("Configuration saved locally. Load a run ID or analyze a repository.", "#526273");
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or ArgumentException)
        {
            SetNotice($"Configuration was not saved. {error.Message}", "#B3261E");
        }
        finally
        {
            _busy = false;
            PopulateState();
            _completion.TrySetResult();
        }
    }

    private void Cancel()
    {
        if (_cancellation is null || _cancelling)
            return;
        _cancelling = true;
        _activeOperation = "Cancellation requested for the owned CLI process tree.";
        _cancellation.Cancel();
        RefreshBindings();
    }

    private void ConfigurationEdited()
    {
        _configurationDirty = true;
        _runFresh = false;
        RefreshBindings();
    }

    private void AppendLog(BackendNotice notice)
    {
        if (Logs.Count == LogLimit)
        {
            Logs.RemoveAt(0);
            _discardedLines++;
        }
        var timestamp = TimeZoneInfo.ConvertTime(DateTimeOffset.UtcNow, EasternTime);
        Logs.Add(new LogRow(timestamp.ToString("h:mm:ss tt"), notice.Source, notice.Message,
            notice.IsError ? "#FFB4AB" : "#DEE7F0"));
        Changed(nameof(LogSummary));
    }

    private void SetNotice(string text, string color)
    {
        _notice = text;
        _noticeColor = color;
        Changed(nameof(Notice));
        Changed(nameof(NoticeColor));
    }

    private void PopulateState()
    {
        var defaults = new[] { ("scout", "Scout"), ("approval", "Approval"), ("baseline", "Unchanged baseline"), ("port", "Port"),
            ("build", "Build"), ("verify", "Verify"), ("evidence", "Evidence") };
        var stages = _run?.Stages?.ToList() ?? [];
        foreach (var (id, title) in defaults)
        {
            if (stages.All(stage => stage.Id != id))
                stages.Add(new RunStage { Id = id, Title = title });
        }
        Stages.Clear();
        for (var index = 0; index < stages.Count; index++)
        {
            var stage = stages[index];
            Stages.Add(new StageRow(stage.Id ?? "unknown", (index + 1).ToString("00"), stage.Title ?? stage.Id ?? "Unknown",
                stage.Status ?? "unknown", stage.Detail ?? "Not reported by the backend."));
        }
        Artifacts.Clear();
        foreach (var (key, title) in new[] { ("root", "Run folder"), ("arm64Package", "ARM64 package"),
            ("x64Package", "x64 package"), ("architectureReport", "Architecture report"), ("patch", "Source patch"),
            ("partialPatch", "Partial source patch"),
            ("evidence", "Evidence export") })
            Artifacts.Add(new ArtifactRow(title, Presentation.Known(_run?.Artifact(key))));
        RefreshBindings();
    }

    private string? OutputPath()
    {
        var path = _run?.Artifact("root");
        return path is not null && LocalPaths.Validate(path) is null && LocalPaths.Validate(_settings.RunsRoot) is null &&
            LocalPaths.IsWithin(path, _settings.RunsRoot) && Directory.Exists(path) ? Path.GetFullPath(path) : null;
    }

    private void OpenOutput()
    {
        var path = OutputPath();
        if (path is null)
            return;
        try
        {
            var info = new ProcessStartInfo
            {
                FileName = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "explorer.exe"),
                UseShellExecute = false
            };
            info.ArgumentList.Add(path);
            using var process = Process.Start(info);
            if (process is null)
                SetNotice("Windows did not accept the output-folder request.", "#B3261E");
        }
        catch (Exception error) when (error is Win32Exception or InvalidOperationException)
        {
            SetNotice($"Could not open the run folder. {error.Message}", "#B3261E");
        }
    }

    private void RefreshBindings()
    {
        Changed(string.Empty);
        foreach (var command in new[] { AnalyzeCommand, RefreshCommand, ApproveCommand, PortCommand,
            BuildCommand, VerifyCommand, InjectFaultCommand, RepairCommand, ExportCommand, SaveSettingsCommand, ResumeCommand })
            command.Refresh();
        CancelCommand.Refresh();
        OpenOutputCommand.Refresh();
    }
}
