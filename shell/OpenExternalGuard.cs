namespace Shell;

/// <summary>
/// System-browser opens must never carry a hub token or hit the hub host.
/// </summary>
internal static class OpenExternalGuard
{
    public static bool Allowed(string? url, string? hubHost)
    {
        var raw = url ?? "";
        var decoded = Decode(raw);
        if (HasTokenParam(raw) || HasTokenParam(decoded))
            return false;
        var host = (hubHost ?? "").Trim();
        if (host.Length == 0)
            return true;
        return !ContainsHost(raw, host) && !ContainsHost(decoded, host);
    }

    internal static string Decode(string raw)
    {
        var cur = raw ?? "";
        for (var i = 0; i < 4; i++)
        {
            string next;
            try
            {
                next = Uri.UnescapeDataString(cur);
            }
            catch (UriFormatException)
            {
                break;
            }
            if (next == cur)
                break;
            cur = next;
        }
        return cur;
    }

    internal static bool HasTokenParam(string text)
    {
        return (text ?? "").IndexOf("token=", StringComparison.OrdinalIgnoreCase) >= 0;
    }

    static bool ContainsHost(string text, string host)
    {
        return (text ?? "").IndexOf(host, StringComparison.OrdinalIgnoreCase) >= 0;
    }
}
