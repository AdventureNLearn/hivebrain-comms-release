using Shell;

var failed = 0;

void Mutant(string name, bool ok)
{
    if (ok)
    {
        Console.WriteLine("ok " + name);
        return;
    }
    failed++;
    Console.WriteLine("FAIL " + name);
}

Mutant("activate-app",
    PopupDismiss.ShouldDismissOnActivateApp(IntPtr.Zero)
    && !PopupDismiss.ShouldDismissOnActivateApp(new IntPtr(1)));

Mutant("deactivate-owner",
    PopupDismiss.ShouldDismissOnDeactivate(false)
    && !PopupDismiss.ShouldDismissOnDeactivate(true));

Mutant("esc",
    PopupDismiss.ShouldHandleEscape(PopupDismiss.EscapeKey)
    && !PopupDismiss.ShouldHandleEscape(0x41));

Mutant("dismiss-all", PopupDismiss.ShouldDismissAllTracked);

Mutant("cleartopmost",
    PopupDismiss.ClearTopMostFlags == (PopupDismiss.SwpNoMove | PopupDismiss.SwpNoSize | PopupDismiss.SwpNoActivate)
    && PopupDismiss.HwndNoTopMost == new IntPtr(-2));

Mutant("autoclose", PopupDismiss.MenuAutoClose);

Mutant("tracking", PopupDismiss.ShouldTrackMenu);

Mutant("contextmenu-hook", PopupDismiss.ShouldHookContextMenuRequested);

Mutant("child-esc", PopupDismiss.ShouldBindChildFormEscape);

Mutant("deferral-itemclicked",
    PopupDismiss.CompleteDeferralOnItemClicked
    && !PopupDismiss.CompleteDeferralOnClosed
    && PopupDismiss.DisposeMenuOnBeginInvoke
    && PopupDismiss.HookNestedDropDownItemClicked);

Mutant("hub-origin",
    PopupDismiss.ShouldAttachHubDismiss("http://127.0.0.1:8107/build.html")
    && !PopupDismiss.ShouldAttachHubDismiss("http://localhost:8107/")
    && !PopupDismiss.ShouldAttachHubDismiss("https://127.0.0.1:8107/")
    && !PopupDismiss.ShouldAttachHubDismiss("http://127.0.0.1:8107@evil/")
    && !PopupDismiss.ShouldAttachHubDismiss("https://grok.com/")
    && !PopupDismiss.ShouldAttachHubDismiss("https://x.com/")
    && !PopupDismiss.ShouldAttachHubDismiss("http://127.0.0.1:80/"));

if (failed != 0)
{
    Console.WriteLine("failed " + failed);
    return 1;
}
Console.WriteLine("11 ok");

var click = new FakeSink();
var menu = new FakeMenu();
var session = new OwnedMenuController(click);
menu.ItemClicked += (tag, sub) => session.OnItemClicked(tag, sub);
menu.Closed += session.OnClosed;
menu.RaiseClick(OwnedMenuController.SubmenuTag, true);
Mutant("controller-skip-submenu",
    !session.Finished && session.SelectedCommandId is null && click.Events.Count == 0);
menu.RaiseClick(7, false);
Mutant("controller-id-before-complete",
    session.Finished
    && session.SelectedCommandId == 7
    && click.Events.Count >= 3
    && click.Events[0] == "id:7"
    && click.Events[1] == "complete"
    && click.Events[2] == "dispose");
var afterClick = click.Events.Count;
menu.RaiseClose();
Mutant("controller-closed-after-click", click.Events.Count == afterClick);

var dismiss = new FakeSink();
var unused = new FakeMenu();
var unusedSession = new OwnedMenuController(dismiss);
unused.ItemClicked += (tag, sub) => unusedSession.OnItemClicked(tag, sub);
unused.Closed += unusedSession.OnClosed;
unused.RaiseClose();
Mutant("controller-closed-completes",
    unusedSession.Finished
    && unusedSession.SelectedCommandId is null
    && dismiss.Events.Count == 2
    && dismiss.Events[0] == "complete"
    && dismiss.Events[1] == "dispose");

var nestedSink = new FakeSink();
var root = new FakeMenu();
var nested = new FakeMenu();
var nestedSession = new OwnedMenuController(nestedSink);
root.ItemClicked += (tag, sub) => nestedSession.OnItemClicked(tag, sub);
nested.ItemClicked += (tag, sub) => nestedSession.OnItemClicked(tag, sub);
root.Closed += nestedSession.OnClosed;
root.RaiseClick(OwnedMenuController.SubmenuTag, true);
Mutant("controller-nested-parent-skipped",
    !nestedSession.Finished && nestedSession.SelectedCommandId is null);
nested.RaiseClick(42, false);
Mutant("controller-nested-click",
    nestedSession.Finished
    && nestedSession.SelectedCommandId == 42
    && nestedSink.Events.Count >= 3
    && nestedSink.Events[0] == "id:42"
    && nestedSink.Events[1] == "complete"
    && nestedSink.Events[2] == "dispose");
var afterNested = nestedSink.Events.Count;
root.RaiseClose();
Mutant("controller-nested-closed-keeps-id",
    nestedSession.SelectedCommandId == 42 && nestedSink.Events.Count == afterNested);

if (failed != 0)
{
    Console.WriteLine("failed " + failed);
    return 1;
}
Console.WriteLine("controller ok");
Console.WriteLine("nested-click ok");
return 0;

sealed class FakeSink : IOwnedMenuSink
{
    public List<string> Events { get; } = new();
    public void SetSelectedCommandId(int commandId) => Events.Add("id:" + commandId);
    public void CompleteDeferral() => Events.Add("complete");
    public void ScheduleDispose() => Events.Add("dispose");
}

sealed class FakeMenu
{
    public event Action<object?, bool>? ItemClicked;
    public event Action? Closed;
    public void RaiseClick(object? tag, bool submenu) => ItemClicked?.Invoke(tag, submenu);
    public void RaiseClose() => Closed?.Invoke();
}
