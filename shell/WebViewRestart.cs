namespace Shell;

/// <summary>
/// Restart a tab after WebView2 ProcessFailed without blocking the UI thread.
/// </summary>
internal static class WebViewRestart
{
    public const int TimeoutMs = 15000;

    public static bool MustRecreateEnvironment(bool envMissing, bool browserProcessDied) =>
        envMissing || browserProcessDied;
}
