using System.Collections.Specialized;
using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace RepoToArm.Workbench;

public partial class MainWindow : Window
{
    private readonly WorkbenchViewModel _viewModel;
    private bool _allowClose;

    public MainWindow(WorkbenchViewModel viewModel)
    {
        InitializeComponent();
        _viewModel = viewModel;
        DataContext = viewModel;
        Loaded += async (_, _) =>
        {
            if (viewModel.ConfigurationOpen)
                ConfigurationSection.BringIntoView();
            await viewModel.RestoreAsync();
        };
        Closing += OnClosing;
        viewModel.Logs.CollectionChanged += OnLogAdded;
    }

    private void OnConfigure(object sender, RoutedEventArgs e)
    {
        _viewModel.ConfigurationOpen = true;
        ConfigurationSection.BringIntoView();
    }

    private void OnLogAdded(object? sender, NotifyCollectionChangedEventArgs e)
    {
        if (e.Action != NotifyCollectionChangedAction.Add || _viewModel.Logs.Count == 0)
            return;
        var scroll = FindScrollViewer(EventList);
        if (scroll is null || scroll.ScrollableHeight - scroll.VerticalOffset < 1)
            EventList.ScrollIntoView(_viewModel.Logs[^1]);
    }

    private async void OnClosing(object? sender, CancelEventArgs e)
    {
        if (_allowClose)
            return;
        if (!_viewModel.IsBusy)
        {
            _viewModel.Logs.CollectionChanged -= OnLogAdded;
            return;
        }
        e.Cancel = true;
        if (_viewModel.IsCommandActive && MessageBox.Show(this, "Stop the owned CLI process tree and close?\n\nRemote runner jobs may continue. " +
            "No completion will be assumed. Reopen the app and refresh the run to check its state.",
            "Command still active", MessageBoxButton.YesNo, MessageBoxImage.Warning, MessageBoxResult.No) != MessageBoxResult.Yes)
            return;
        await _viewModel.StopAsync();
        _allowClose = true;
        _viewModel.Logs.CollectionChanged -= OnLogAdded;
        Close();
    }

    private static ScrollViewer? FindScrollViewer(DependencyObject parent)
    {
        for (var index = 0; index < VisualTreeHelper.GetChildrenCount(parent); index++)
        {
            var child = VisualTreeHelper.GetChild(parent, index);
            if (child is ScrollViewer scroll)
                return scroll;
            var nested = FindScrollViewer(child);
            if (nested is not null)
                return nested;
        }
        return null;
    }
}
