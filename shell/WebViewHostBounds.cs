using System.Drawing;

namespace Shell;

/// <summary>
/// Target WebView2 size from the tab host. Dock toggles that land on the
/// same Size do not raise SizeChanged, so callers must apply a 1px nudge
/// when current == target, then restore.
/// </summary>
internal static class WebViewHostBounds
{
    public static Size TabHostSize(Size formClient, int topChrome, int bottomChrome)
    {
        var width = Math.Max(formClient.Width, 1);
        var height = Math.Max(formClient.Height - Math.Max(topChrome, 0) - Math.Max(bottomChrome, 0), 1);
        return new Size(width, height);
    }

    public static Rectangle FillBounds(Size pageClient, int topReserve)
    {
        var top = Math.Max(0, Math.Min(topReserve, Math.Max(pageClient.Height - 1, 0)));
        var width = Math.Max(pageClient.Width, 1);
        var height = Math.Max(pageClient.Height - top, 1);
        return new Rectangle(0, top, width, height);
    }

    public static Size NudgeIfUnchanged(Size current, Size target)
    {
        if (current.Width == target.Width && current.Height == target.Height)
            return new Size(Math.Max(target.Width - 1, 1), target.Height);
        return target;
    }

    public static readonly string[] AllTabs = { "SuperGrok", "X", "Imagine", "Build", "Control" };

    /// <summary>
    /// Layout size for every tab after minimize, maximize, or a user resize.
    /// Minimized uses the restored client size so tabs stay non-zero.
    /// </summary>
    public static Size ClientForWindowState(Size restoredClient, Size maximizedClient, string windowState)
    {
        if (windowState == "maximized") return maximizedClient;
        return new Size(Math.Max(restoredClient.Width, 1), Math.Max(restoredClient.Height, 1));
    }

    public static Size TabSizeAfterWindowChange(Size restoredClient, Size maximizedClient, string windowState, int topChrome, int bottomChrome)
    {
        var client = ClientForWindowState(restoredClient, maximizedClient, windowState);
        return TabHostSize(client, topChrome, bottomChrome);
    }
}
