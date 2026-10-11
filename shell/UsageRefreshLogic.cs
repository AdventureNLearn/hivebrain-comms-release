using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Shell;

/// <summary>
/// Usage refresh: API first, then captured rate-limit headers, then a read-only DOM read.
/// Parsers never guess. Manual typed limits win. Update selectors in docs/USAGE-REFRESH.md.
/// </summary>
internal static class UsageRefreshLogic
{
    public const int MinIntervalMinutes = 5;
    public const int TabTimeoutMs = 5000;
    public const string NotAvailable = "not available";
    public const string SourceApi = "API";
    public const string SourceHeader = "header";
    public const string SourcePage = "page";
    public const string SourceManual = "manual";
    public const string XApiUrl = "https://api.x.com/2/usage/tweets";
    public const string XApiLabel = "X API project usage";
    public const string XApiDocs = "https://docs.x.com/x-api/usage/introduction";
    public const string XApiRef = "https://developer.twitter.com/en/docs/twitter-api/usage/tweets/api-reference/get-usage-tweets";
    public const int SourceTextCap = 240;

    public readonly record struct RateLimitEndpoint(string Host, string Path);

    public static readonly RateLimitEndpoint[] RateLimitEndpoints =
    {
        new("api.x.com", "/2/usage/tweets"),
    };

    public sealed class Reading
    {
        public string? Used;
        public string? Limit;
        public string? ResetsAt;
        public string Source = "";
        public string SourceText = "";
        public string Status = "absent";
    }

    public sealed class HeaderSnap
    {
        public string? Limit;
        public string? Remaining;
        public string? Reset;
        public bool HasAny => Limit != null || Remaining != null || Reset != null;
    }

    public sealed class DisplayRow
    {
        public string Id = "";
        public string Label = "";
        public string Used = NotAvailable;
        public string Limit = NotAvailable;
        public string Reset = NotAvailable;
        public string Source = NotAvailable;
        public string ReadAt = NotAvailable;
    }

    public sealed class SiteConfig
    {
        public string Id = "";
        public string[] Selectors = Array.Empty<string>();
        public string Pattern = "";
        public string Docs = "";
    }

    // Conservative visible-text hooks. Default is not available when they miss.
    // How to update: docs/USAGE-REFRESH.md
    public static readonly IReadOnlyDictionary<string, SiteConfig> Sites = new Dictionary<string, SiteConfig>(StringComparer.Ordinal)
    {
        ["supergrok"] = new SiteConfig
        {
            Id = "supergrok",
            Selectors = new[] { "[data-testid*='usage']", "[aria-label*='usage']", "[class*='usage']" },
            Pattern = @"(\d+)\s*(?:of|/)\s*(\d+)",
            Docs = "No public SuperGrok usage API. DOM fallback only. https://grok.com/",
        },
        ["imagine"] = new SiteConfig
        {
            Id = "imagine",
            Selectors = new[] { "[data-testid*='usage']", "[aria-label*='usage']", "[class*='usage']" },
            Pattern = @"(\d+)\s*(?:of|/)\s*(\d+)",
            Docs = "No public Imagine usage API. DOM fallback only. https://grok.com/imagine",
        },
        ["x-reads"] = new SiteConfig
        {
            Id = "x-reads",
            Selectors = new[] { "[data-testid*='usage']", "[aria-label*='usage']", "[class*='usage']" },
            Pattern = @"(\d+)\s*(?:of|/)\s*(\d+)",
            Docs = "No public X Premium in-app reads API. DOM/header fallback. https://x.com/settings",
        },
        ["x-posts"] = new SiteConfig
        {
            Id = "x-posts",
            Selectors = new[] { "[data-testid*='usage']", "[aria-label*='usage']", "[class*='usage']" },
            Pattern = @"(\d+)\s*(?:of|/)\s*(\d+)",
            Docs = XApiDocs,
        },
        ["build-cli"] = new SiteConfig
        {
            Id = "build-cli",
            Selectors = Array.Empty<string>(),
            Pattern = "",
            Docs = "Local hub GET /api/state only.",
        },
    };

    static readonly Regex SuperGrokPat = new(@"(\d+)\s*(?:of|/)\s*(\d+)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
    static readonly Regex ImaginePat = new(@"(\d+)\s*(?:of|/)\s*(\d+)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
    static readonly Regex XReadsPat = new(@"(\d+)\s*(?:of|/)\s*(\d+)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
    static readonly Regex XPostsPat = new(@"(\d+)\s*(?:of|/)\s*(\d+)", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    public static int ClampIntervalMinutes(int raw) => raw <= 0 ? 0 : Math.Max(MinIntervalMinutes, raw);

    public static bool IntervalOffByDefault(int stored) => ClampIntervalMinutes(stored) == 0;

    public static bool ShouldRunInterval(int minutes, bool minimized, DateTimeOffset last, DateTimeOffset now)
    {
        var every = ClampIntervalMinutes(minutes);
        if (every < MinIntervalMinutes) return false;
        if (minimized) return false;
        if (last == DateTimeOffset.MinValue) return true;
        return now - last >= TimeSpan.FromMinutes(every);
    }

    public static bool RateLimitEndpointOk(string? url)
    {
        if (!Uri.TryCreate((url ?? "").Trim(), UriKind.Absolute, out var uri)) return false;
        if (!string.Equals(uri.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase))
            return false;
        if (!string.IsNullOrEmpty(uri.UserInfo)) return false;
        if (uri.Port != 443) return false;
        var host = (uri.Host ?? "").Trim().TrimEnd('.');
        var path = uri.AbsolutePath.TrimEnd('/');
        if (path.Length == 0) path = "/";
        foreach (var item in RateLimitEndpoints)
        {
            var want = (item.Path ?? "").TrimEnd('/');
            if (want.Length == 0) want = "/";
            if (host.Equals(item.Host, StringComparison.OrdinalIgnoreCase)
                && path.Equals(want, StringComparison.Ordinal))
                return true;
        }
        return false;
    }

    public static HeaderSnap ReadRateLimitHeaders(Func<string, string?> get)
    {
        var snap = new HeaderSnap
        {
            Limit = NumericToken(get("x-ratelimit-limit") ?? get("X-RateLimit-Limit")),
            Remaining = NumericToken(get("x-ratelimit-remaining") ?? get("X-RateLimit-Remaining")),
            Reset = NumericToken(get("x-ratelimit-reset") ?? get("X-RateLimit-Reset")),
        };
        return snap;
    }

    public static Reading FromHeaders(HeaderSnap? snap)
    {
        var reading = new Reading { Source = SourceHeader, Status = "absent" };
        if (snap is null || !snap.HasAny) return reading;
        reading.Limit = snap.Limit;
        reading.ResetsAt = snap.Reset;
        if (snap.Limit != null && snap.Remaining != null
            && long.TryParse(snap.Limit, NumberStyles.None, CultureInfo.InvariantCulture, out var cap)
            && long.TryParse(snap.Remaining, NumberStyles.None, CultureInfo.InvariantCulture, out var left)
            && cap >= left)
        {
            reading.Used = (cap - left).ToString(CultureInfo.InvariantCulture);
        }
        reading.Status = reading.Used != null || reading.Limit != null ? "ok" : "absent";
        reading.SourceText = "rate-limit header";
        return reading;
    }

    public static Reading ParseXApi(string json)
    {
        var reading = new Reading { Source = SourceApi, SourceText = XApiLabel, Status = "absent" };
        try
        {
            using var doc = JsonDocument.Parse(json ?? "");
            if (!doc.RootElement.TryGetProperty("data", out var data) || data.ValueKind != JsonValueKind.Object)
            {
                reading.Status = "changed";
                return reading;
            }
            reading.Used = NumberProp(data, "project_usage");
            reading.Limit = NumberProp(data, "project_cap");
            var reset = NumberProp(data, "cap_reset_day");
            if (reset != null) reading.ResetsAt = reset;
            reading.Status = reading.Used != null || reading.Limit != null ? "ok" : "absent";
        }
        catch (JsonException)
        {
            reading.Status = "changed";
        }
        return reading;
    }

    public static Reading ParseSuperGrokDom(string payload) => ParseDom(payload, SuperGrokPat, "supergrok");
    public static Reading ParseImagineDom(string payload) => ParseDom(payload, ImaginePat, "imagine");
    public static Reading ParseXReadsDom(string payload) => ParseDom(payload, XReadsPat, "x-reads");
    public static Reading ParseXPostsDom(string payload) => ParseDom(payload, XPostsPat, "x-posts");

    public static Reading ParseBuildState(string json)
    {
        var reading = new Reading { Source = SourceApi, SourceText = "hub state", Status = "absent" };
        try
        {
            using var doc = JsonDocument.Parse(json ?? "");
            var root = doc.RootElement;
            if (root.TryGetProperty("sessions", out var sessions) && sessions.ValueKind == JsonValueKind.Array)
            {
                reading.Used = sessions.GetArrayLength().ToString(CultureInfo.InvariantCulture);
                reading.Status = "ok";
            }
            else if (root.TryGetProperty("queue", out var queue) && queue.ValueKind == JsonValueKind.Array)
            {
                reading.Used = queue.GetArrayLength().ToString(CultureInfo.InvariantCulture);
                reading.Status = "ok";
            }
        }
        catch (JsonException)
        {
            reading.Status = "changed";
        }
        return reading;
    }

    public static Reading ParseDom(string payload, Regex pattern, string site)
    {
        var reading = new Reading { Source = SourcePage, Status = "absent" };
        var text = ExtractSourceText(payload);
        reading.SourceText = text.Length > SourceTextCap ? text.Substring(0, SourceTextCap) : text;
        var used = JsonStringField(payload, "used");
        var limit = JsonStringField(payload, "limit");
        var resets = JsonStringField(payload, "resetsAt");
        if (NumericToken(used) != null) reading.Used = NumericToken(used);
        if (NumericToken(limit) != null) reading.Limit = NumericToken(limit);
        if (!string.IsNullOrWhiteSpace(resets)) reading.ResetsAt = resets.Trim();
        if (reading.Used == null && reading.Limit == null && text.Length > 0)
        {
            var match = pattern.Match(text);
            if (match.Success)
            {
                reading.Used = match.Groups[1].Value;
                reading.Limit = match.Groups[2].Value;
            }
        }
        if (reading.Used == null && reading.Limit == null)
            reading.Status = text.Length == 0 ? "absent" : "changed";
        else
            reading.Status = "ok";
        return reading;
    }

    public static DisplayRow Merge(string id, string label, Reading? found, UsageStore.Row? manual, DateTimeOffset now)
    {
        var row = new DisplayRow { Id = id, Label = label, ReadAt = now.ToString("HH:mm:ss", CultureInfo.InvariantCulture) };
        var manualLimit = UsageStore.DisplayOrBlank(manual?.Limit);
        var used = found?.Used;
        var limit = found?.Limit;
        var reset = found?.ResetsAt;
        row.Used = string.IsNullOrEmpty(used) ? NotAvailable : used;
        row.Limit = manualLimit.Length > 0 ? manualLimit : (string.IsNullOrEmpty(limit) ? NotAvailable : limit);
        row.Reset = string.IsNullOrEmpty(reset) ? NotAvailable : reset;
        if (found != null && found.Status == "ok" && !string.IsNullOrEmpty(found.Source))
            row.Source = manualLimit.Length > 0 ? found.Source + "+" + SourceManual : found.Source;
        else if (manualLimit.Length > 0)
            row.Source = SourceManual;
        else
            row.Source = NotAvailable;
        return row;
    }

    public static Reading Prefer(params Reading?[] readings)
    {
        foreach (var item in readings)
        {
            if (item != null && item.Status == "ok")
                return item;
        }
        return new Reading { Status = "absent" };
    }

    public static string DomScript(string site)
    {
        Sites.TryGetValue(site, out var cfg);
        var selectors = cfg?.Selectors ?? Array.Empty<string>();
        var list = string.Join(",", selectors.Select(item => JsonSerializer.Serialize(item)));
        return "(function(){var s=[" + list + "];var t=[];var i,j,n,x;"
            + "for(i=0;i<s.length;i++){n=document.querySelectorAll(s[i]);"
            + "for(j=0;j<n.length&&t.length<8;j++){x=((n[j].innerText||n[j].textContent||'')+'').replace(/\\s+/g,' ').trim();"
            + "if(x)t.push(x.slice(0,120));}}"
            + "return JSON.stringify({used:null,limit:null,resetsAt:null,sourceText:t.join(' | ').slice(0,240)});})();";
    }

    public static readonly string[] ScriptBanned =
    {
        "click(", "submit(", "fetch(", "XMLHttpRequest", "location.assign", "location.href",
        "sendBeacon", "postMessage", ".post(", "PUT", "DELETE",
    };

    public static bool ScriptIsReadOnly(string script)
    {
        var raw = script ?? "";
        foreach (var banned in ScriptBanned)
        {
            if (raw.IndexOf(banned, StringComparison.OrdinalIgnoreCase) >= 0)
                return false;
        }
        return raw.Contains("querySelectorAll", StringComparison.Ordinal)
            && raw.Contains("innerText", StringComparison.Ordinal);
    }

    public static string LogLine(string site, string status)
    {
        var safeSite = (site ?? "").Trim();
        var safeStatus = (status ?? "").Trim();
        if (safeSite.Length > 24) safeSite = safeSite.Substring(0, 24);
        if (safeStatus.Length > 24) safeStatus = safeStatus.Substring(0, 24);
        if (LooksSecret(safeSite) || LooksSecret(safeStatus))
            return "usage parse redacted";
        return "usage parse " + safeSite + " " + safeStatus;
    }

    public static bool LooksSecret(string text)
    {
        var raw = text ?? "";
        if (raw.IndexOf("bearer", StringComparison.OrdinalIgnoreCase) >= 0) return true;
        if (raw.IndexOf("token=", StringComparison.OrdinalIgnoreCase) >= 0) return true;
        if (raw.IndexOf("cookie", StringComparison.OrdinalIgnoreCase) >= 0) return true;
        if (raw.IndexOf("<html", StringComparison.OrdinalIgnoreCase) >= 0) return true;
        if (raw.StartsWith("http://", StringComparison.OrdinalIgnoreCase)) return true;
        if (raw.StartsWith("https://", StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }

    static string ExtractSourceText(string payload)
    {
        var field = JsonStringField(payload, "sourceText");
        if (field != null) return field;
        var unwrapped = UnwrapJsonString(payload);
        field = JsonStringField(unwrapped, "sourceText");
        if (field != null) return field;
        return (payload ?? "").Trim();
    }

    static string? JsonStringField(string payload, string name)
    {
        foreach (var raw in new[] { payload ?? "", UnwrapJsonString(payload ?? "") })
        {
            try
            {
                using var doc = JsonDocument.Parse(raw);
                if (doc.RootElement.ValueKind != JsonValueKind.Object) continue;
                if (!doc.RootElement.TryGetProperty(name, out var el)) continue;
                if (el.ValueKind == JsonValueKind.String) return el.GetString();
                if (el.ValueKind == JsonValueKind.Number) return el.GetRawText();
                if (el.ValueKind == JsonValueKind.Null) return null;
            }
            catch (JsonException)
            {
            }
        }
        return null;
    }

    static string UnwrapJsonString(string payload)
    {
        var raw = (payload ?? "").Trim();
        if (raw.Length >= 2 && raw[0] == '"')
        {
            try { return JsonSerializer.Deserialize<string>(raw) ?? raw; }
            catch (JsonException) { }
        }
        return raw;
    }

    static string? NumberProp(JsonElement obj, string name)
    {
        if (!obj.TryGetProperty(name, out var el)) return null;
        if (el.ValueKind == JsonValueKind.Number) return el.GetRawText();
        if (el.ValueKind == JsonValueKind.String) return NumericToken(el.GetString());
        return null;
    }

    public static string? NumericToken(string? raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return null;
        var text = raw.Trim();
        if (text.Length == 0 || text.Length > 18) return null;
        foreach (var ch in text)
            if (!char.IsDigit(ch)) return null;
        return text;
    }
}
