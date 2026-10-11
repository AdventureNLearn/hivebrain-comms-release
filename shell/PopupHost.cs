using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Shell;

public partial class Form1
{
    private readonly List<ToolStripDropDown> trackedMenus = new();
    private readonly List<WebView2> webViews = new();
    private const string HubDismissScript =
        "(function(){function send(){try{window.chrome.webview.postMessage('hbc-dismiss')}catch(e){}}" +
        "if(window.__hbcDismissBound)return;window.__hbcDismissBound=true;" +
        "window.addEventListener('blur',send);" +
        "document.addEventListener('visibilitychange',function(){if(document.hidden)send();});" +
        "document.addEventListener('keydown',function(e){if(e.key==='Escape')send();});" +
        "document.addEventListener('pointerdown',send,true);})();";

    private void InitPopupDismiss()
    {
        KeyPreview = true;
        Deactivate += (_, _) => BeginInvoke(DismissIfAppLostFocus);
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == PopupDismiss.WmActivateApp && PopupDismiss.ShouldDismissOnActivateApp(m.WParam))
            DismissPopups();
        base.WndProc(ref m);
    }

    protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
    {
        if (PopupDismiss.ShouldHandleEscape((int)keyData) && DismissPopups())
            return true;
        if (TrySelectTabShortcut(keyData))
            return true;
        return base.ProcessCmdKey(ref msg, keyData);
    }

    private void DismissIfAppLostFocus()
    {
        if (IsDisposed) return;
        if (PopupDismiss.ShouldDismissOnDeactivate(ForegroundIsThisApp()))
            DismissPopups();
    }

    private static bool ForegroundIsThisApp()
    {
        var hwnd = NativeMethods.GetForegroundWindow();
        if (hwnd == IntPtr.Zero) return false;
        NativeMethods.GetWindowThreadProcessId(hwnd, out var pid);
        return pid == (uint)Environment.ProcessId;
    }

    private bool DismissPopups()
    {
        if (!PopupDismiss.ShouldDismissAllTracked) return false;
        var closed = false;
        foreach (var menu in trackedMenus.ToArray())
        {
            if (menu.IsDisposed || !menu.Visible) continue;
            menu.Close();
            closed = true;
        }
        return closed;
    }

    private void TrackMenu(ToolStripDropDown menu)
    {
        if (!PopupDismiss.ShouldTrackMenu) return;
        menu.AutoClose = PopupDismiss.MenuAutoClose;
        if (trackedMenus.Contains(menu)) return;
        ClearTopMost(menu);
        trackedMenus.Add(menu);
        menu.Closed += (_, _) => trackedMenus.Remove(menu);
    }

    private sealed class OwnedMenu : ContextMenuStrip
    {
        public OwnedMenu()
        {
            AutoClose = PopupDismiss.MenuAutoClose;
            ShowImageMargin = false;
        }
    }

    private static void ClearTopMost(ToolStripDropDown menu)
    {
        void Apply()
        {
            if (menu.IsDisposed || !menu.IsHandleCreated) return;
            NativeMethods.SetWindowPos(
                menu.Handle,
                PopupDismiss.HwndNoTopMost,
                0, 0, 0, 0,
                PopupDismiss.ClearTopMostFlags);
        }
        if (menu.IsHandleCreated) Apply();
        menu.Opened += (_, _) => Apply();
    }

    private ContextMenuStrip NewOwnedMenu()
    {
        var menu = new OwnedMenu();
        TrackMenu(menu);
        return menu;
    }

    private async Task AttachOwnedWebView(WebView2 view)
    {
        webViews.Add(view);
        view.Disposed += (_, _) => webViews.Remove(view);
        view.CoreWebView2.Settings.AreDefaultContextMenusEnabled = true;
        if (PopupDismiss.ShouldHookContextMenuRequested)
            view.CoreWebView2.ContextMenuRequested += (_, e) => OnWebViewContextMenu(view, e);
        view.CoreWebView2.NavigationCompleted += (_, _) => AttachHubDismiss(view);
        view.CoreWebView2.WebMessageReceived += OnWebMessage;
        AttachHubDismiss(view);
        await Task.CompletedTask;
    }

    private static void AttachHubDismiss(WebView2 view)
    {
        if (view.IsDisposed || view.CoreWebView2 is null) return;
        var source = view.CoreWebView2.Source;
        if (!PopupDismiss.ShouldAttachHubDismiss(source)) return;
        _ = view.CoreWebView2.ExecuteScriptAsync(HubDismissScript);
    }

    private void OnWebMessage(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
    {
        var view = webViews.FirstOrDefault(v => !v.IsDisposed && v.CoreWebView2 == sender);
        var source = view?.CoreWebView2?.Source;
        if (!PopupDismiss.ShouldAttachHubDismiss(source)) return;
        try
        {
            if (e.TryGetWebMessageAsString() == "hbc-dismiss")
                DismissPopups();
        }
        catch (ArgumentException)
        {
        }
    }

    private void OnWebViewContextMenu(WebView2 view, CoreWebView2ContextMenuRequestedEventArgs e)
    {
        e.Handled = true;
        var deferral = e.GetDeferral();
        var menu = NewOwnedMenu();
        foreach (var item in e.MenuItems)
            menu.Items.Add(WrapMenuItem(item));
        var session = new OwnedMenuController(new WebView2MenuSink(this, e, deferral, menu));
        HookOwnedMenuClicks(menu, session);
        menu.Closed += (_, _) => session.OnClosed();
        if (view.IsDisposed)
        {
            session.Finish();
            return;
        }
        menu.Show(view, e.Location);
        ClearTopMost(menu);
    }

    private void HookOwnedMenuClicks(ToolStripDropDown drop, OwnedMenuController session)
    {
        drop.ItemClicked += (_, ev) =>
        {
            var clicked = ev.ClickedItem as ToolStripItem;
            var submenu = clicked is ToolStripMenuItem parent && parent.DropDownItems.Count > 0;
            session.OnItemClicked(clicked?.Tag, submenu);
        };
        if (!PopupDismiss.HookNestedDropDownItemClicked) return;
        foreach (ToolStripItem item in drop.Items)
        {
            if (item is not ToolStripMenuItem entry || entry.DropDownItems.Count == 0) continue;
            HookOwnedMenuClicks(entry.DropDown, session);
        }
    }

    private ToolStripItem WrapMenuItem(CoreWebView2ContextMenuItem item)
    {
        if (item.Kind == CoreWebView2ContextMenuItemKind.Separator)
            return new ToolStripSeparator();
        var entry = new ToolStripMenuItem(item.Label)
        {
            Enabled = item.IsEnabled,
            Checked = item.IsChecked,
        };
        if (item.Kind == CoreWebView2ContextMenuItemKind.Submenu)
        {
            entry.Tag = OwnedMenuController.SubmenuTag;
            foreach (var child in item.Children)
                entry.DropDownItems.Add(WrapMenuItem(child));
            entry.DropDown.AutoClose = PopupDismiss.MenuAutoClose;
            ClearTopMost(entry.DropDown);
            TrackMenu(entry.DropDown);
        }
        else
        {
            entry.Tag = item.CommandId;
        }
        return entry;
    }

    private sealed class WebView2MenuSink : IOwnedMenuSink
    {
        private readonly Control marshal;
        private readonly CoreWebView2ContextMenuRequestedEventArgs request;
        private readonly CoreWebView2Deferral deferral;
        private readonly ContextMenuStrip menu;

        public WebView2MenuSink(
            Control marshal,
            CoreWebView2ContextMenuRequestedEventArgs request,
            CoreWebView2Deferral deferral,
            ContextMenuStrip menu)
        {
            this.marshal = marshal;
            this.request = request;
            this.deferral = deferral;
            this.menu = menu;
        }

        public void SetSelectedCommandId(int commandId)
        {
            try { request.SelectedCommandId = commandId; }
            catch (InvalidOperationException) { }
        }

        public void CompleteDeferral() => deferral.Complete();

        public void ScheduleDispose()
        {
            void DisposeNow()
            {
                if (!menu.IsDisposed) menu.Dispose();
            }
            if (PopupDismiss.DisposeMenuOnBeginInvoke
                && !marshal.IsDisposed
                && marshal.IsHandleCreated
                && !marshal.Disposing)
            {
                try
                {
                    marshal.BeginInvoke((Action)DisposeNow);
                    return;
                }
                catch (InvalidOperationException)
                {
                }
            }
            DisposeNow();
        }
    }
}
