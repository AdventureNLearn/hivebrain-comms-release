using System.Globalization;
using System.Text.RegularExpressions;

namespace Shell;

public static class PopupDismiss
{
    public const int WmActivateApp = 0x001C;
    public const int EscapeKey = 0x1B;
    public const uint SwpNoMove = 0x0002;
    public const uint SwpNoSize = 0x0001;
    public const uint SwpNoActivate = 0x0010;
    public static readonly IntPtr HwndNoTopMost = new(-2);
    public static readonly int DefaultHubPort = 8107;

    // HBC_HUB_PORT is process-only. Never setx. ASCII [0-9] only.
    public static int ReadHubPort()
    {
        var raw = Environment.GetEnvironmentVariable("HBC_HUB_PORT");
        if (string.IsNullOrWhiteSpace(raw)) return DefaultHubPort;
        var trimmed = raw.Trim();
        if (!Regex.IsMatch(trimmed, "^[0-9]+$")
            || !int.TryParse(trimmed, NumberStyles.None, CultureInfo.InvariantCulture, out var port)
            || port < 1024 || port > 65535)
            return DefaultHubPort;
        return port;
    }

    public static bool ShouldDismissOnActivateApp(IntPtr wParam) => wParam == IntPtr.Zero;

    public static bool ShouldDismissOnDeactivate(bool foregroundIsThisApp) => !foregroundIsThisApp;

    public static bool ShouldHandleEscape(int keyData) => keyData == EscapeKey;

    public static bool ShouldDismissAllTracked => true;

    public static bool MenuAutoClose => true;

    public static bool ShouldTrackMenu => true;

    public static bool ShouldHookContextMenuRequested => true;

    public static bool ShouldBindChildFormEscape => true;

    public static bool CompleteDeferralOnItemClicked => true;

    public static bool CompleteDeferralOnClosed => false;

    public static bool DisposeMenuOnBeginInvoke => true;

    public static bool HookNestedDropDownItemClicked => true;

    public static uint ClearTopMostFlags => SwpNoMove | SwpNoSize | SwpNoActivate;

    public static bool ShouldAttachHubDismiss(string? source)
    {
        if (string.IsNullOrEmpty(source)) return false;
        if (!Uri.TryCreate(source, UriKind.Absolute, out var uri)) return false;
        if (uri.Scheme != Uri.UriSchemeHttp) return false;
        if (uri.Host != "127.0.0.1") return false;
        if (uri.Port != ReadHubPort()) return false;
        if (!string.IsNullOrEmpty(uri.UserInfo)) return false;
        return true;
    }
}
