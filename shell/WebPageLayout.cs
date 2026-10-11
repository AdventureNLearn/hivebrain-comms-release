namespace Shell;

/// <summary>
/// Web tab stack: view, page bar, overlay. AddWeb must add every child.
/// </summary>
internal static class WebPageLayout
{
    public static readonly string[] Names = { "view", "bar", "state" };

    public static T[] Children<T>(T view, T bar, T state) => new[] { view, bar, state };
}
