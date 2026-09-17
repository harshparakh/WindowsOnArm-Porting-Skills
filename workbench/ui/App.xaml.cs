using System.IO;
using System.Text.Json;
using System.Windows;

namespace RepoToArm.Workbench;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        var store = new SettingsStore();
        WorkbenchSettings settings = new();
        string? warning = null;
        try
        {
            settings = store.Load();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException)
        {
            warning = $"Configuration could not be read. It has not been overwritten. {error.Message}";
        }

        try
        {
            settings = WorkbenchSettings.ApplyArguments(settings, e.Args);
        }
        catch (ArgumentException error)
        {
            warning = error.Message;
        }

        var viewModel = new WorkbenchViewModel(settings, store, warning);
        MainWindow = new MainWindow(viewModel);
        MainWindow.Show();
    }
}
