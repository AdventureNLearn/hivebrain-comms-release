using System.Drawing;
using Shell;

var fails = 0;

void Check(string name, Size got, Size want)
{
    if (got == want) return;
    fails += 1;
    Console.Error.WriteLine("FAIL {0}: got {1}x{2} want {3}x{4}", name, got.Width, got.Height, want.Width, want.Height);
}

void CheckRect(string name, Rectangle got, Rectangle want)
{
    if (got == want) return;
    fails += 1;
    Console.Error.WriteLine("FAIL {0}: got {1} want {2}", name, got, want);
}

Check("host-1280", WebViewHostBounds.TabHostSize(new Size(1280, 860), 80, 24), new Size(1280, 756));
Check("host-tiny", WebViewHostBounds.TabHostSize(new Size(0, 0), 80, 24), new Size(1, 1));
Check("nudge-same", WebViewHostBounds.NudgeIfUnchanged(new Size(800, 600), new Size(800, 600)), new Size(799, 600));
Check("nudge-change", WebViewHostBounds.NudgeIfUnchanged(new Size(10, 10), new Size(800, 600)), new Size(800, 600));
CheckRect("fill-banner", WebViewHostBounds.FillBounds(new Size(1280, 756), 24), new Rectangle(0, 24, 1280, 732));
CheckRect("fill-none", WebViewHostBounds.FillBounds(new Size(1280, 756), 0), new Rectangle(0, 0, 1280, 756));

foreach (var tab in WebViewHostBounds.AllTabs)
{
    var create = WebViewHostBounds.TabHostSize(new Size(1280, 860), 88, 26);
    if (create.Width < 1 || create.Height < 1)
    {
        fails += 1;
        Console.Error.WriteLine("FAIL {0}-create-nonzero: {1}x{2}", tab, create.Width, create.Height);
    }
    var banner = tab == "Build" ? 24 : (tab == "Control" ? 0 : 36);
    var shown = WebViewHostBounds.FillBounds(create, banner);
    if (shown.Width < 1 || shown.Height < 1)
    {
        fails += 1;
        Console.Error.WriteLine("FAIL {0}-show-nonzero: {1}", tab, shown);
    }
    var nudged = WebViewHostBounds.NudgeIfUnchanged(create, shown.Size);
    var resized = nudged != create || shown.Size != create;
    if (!resized)
    {
        fails += 1;
        Console.Error.WriteLine("FAIL {0}-resized-on-show: create {1} shown {2} nudge {3}", tab, create, shown.Size, nudged);
    }
}

foreach (var state in new[] { "minimized", "maximized", "resized" })
{
    var restored = new Size(1280, 860);
    var maximized = new Size(1920, 1080);
    var host = WebViewHostBounds.TabSizeAfterWindowChange(restored, maximized, state, 88, 26);
    if (state == "resized")
        host = WebViewHostBounds.TabHostSize(new Size(1100, 700), 88, 26);
    foreach (var tab in WebViewHostBounds.AllTabs)
    {
        if (host.Width >= 1 && host.Height >= 1) continue;
        fails += 1;
        Console.Error.WriteLine("FAIL {0}-{1}-nonzero: {2}x{3}", state, tab, host.Width, host.Height);
    }
}

if (fails != 0)
{
    Console.Error.WriteLine("{0} mutant(s)", fails);
    return 1;
}
Console.WriteLine("webview-bounds-ok");
return 0;
