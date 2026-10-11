using Shell;

var fails = 0;
var root = Directory.GetCurrentDirectory();
if (!Directory.Exists(Path.Combine(root, "tests", "fixtures", "usage")))
    root = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", ".."));
var fix = Path.Combine(root, "tests", "fixtures", "usage");

void Ok(string name, bool cond)
{
    if (cond) return;
    fails += 1;
    Console.Error.WriteLine("FAIL {0}", name);
}

void Eq(string name, string? got, string? want)
{
    if (got == want) return;
    fails += 1;
    Console.Error.WriteLine("FAIL {0}: got {1} want {2}", name, got, want);
}

var sgPresent = UsageRefreshLogic.ParseSuperGrokDom(File.ReadAllText(Path.Combine(fix, "supergrok-present.html")));
Eq("sg-used", sgPresent.Used, "12");
Eq("sg-limit", sgPresent.Limit, "100");
Eq("sg-status", sgPresent.Status, "ok");
Eq("sg-src", sgPresent.Source, UsageRefreshLogic.SourcePage);

var sgAbsent = UsageRefreshLogic.ParseSuperGrokDom("{\"sourceText\":\"\"}");
Eq("sg-absent", sgAbsent.Status, "absent");

var sgChanged = UsageRefreshLogic.ParseSuperGrokDom(File.ReadAllText(Path.Combine(fix, "supergrok-changed.html")));
Eq("sg-changed", sgChanged.Status, "changed");
if (sgChanged.Used != null) { fails += 1; Console.Error.WriteLine("FAIL sg-changed-used"); }

var imPresent = UsageRefreshLogic.ParseImagineDom(File.ReadAllText(Path.Combine(fix, "imagine-present.html")));
Eq("im-used", imPresent.Used, "3");
Eq("im-limit", imPresent.Limit, "20");

var imChanged = UsageRefreshLogic.ParseImagineDom(File.ReadAllText(Path.Combine(fix, "imagine-changed.html")));
Eq("im-changed", imChanged.Status, "changed");

var xPresent = UsageRefreshLogic.ParseXPostsDom(File.ReadAllText(Path.Combine(fix, "x-present.html")));
Eq("x-used", xPresent.Used, "40");
Eq("x-limit", xPresent.Limit, "500");

var xApi = UsageRefreshLogic.ParseXApi(File.ReadAllText(Path.Combine(fix, "x-api-present.json")));
Eq("api-used", xApi.Used, "12");
Eq("api-limit", xApi.Limit, "10000");
Eq("api-src", xApi.Source, UsageRefreshLogic.SourceApi);
Eq("api-label", xApi.SourceText, UsageRefreshLogic.XApiLabel);

var xApiAbsent = UsageRefreshLogic.ParseXApi(File.ReadAllText(Path.Combine(fix, "x-api-absent.json")));
Eq("api-absent", xApiAbsent.Status, "absent");

var xApiChanged = UsageRefreshLogic.ParseXApi(File.ReadAllText(Path.Combine(fix, "x-api-changed.json")));
Eq("api-changed", xApiChanged.Status, "changed");

var headers = System.Text.Json.JsonDocument.Parse(File.ReadAllText(Path.Combine(fix, "headers-present.json"))).RootElement;
string? Get(string name) => headers.TryGetProperty(name, out var el) ? el.GetString() : null;
var snap = UsageRefreshLogic.ReadRateLimitHeaders(Get);
var fromHead = UsageRefreshLogic.FromHeaders(snap);
Eq("hdr-used", fromHead.Used, "50");
Eq("hdr-limit", fromHead.Limit, "200");
Eq("hdr-src", fromHead.Source, UsageRefreshLogic.SourceHeader);

var build = UsageRefreshLogic.ParseBuildState(File.ReadAllText(Path.Combine(fix, "build-state.json")));
Eq("build-used", build.Used, "2");
Eq("build-src", build.Source, UsageRefreshLogic.SourceApi);

var manual = new UsageStore.Row { Feature = "supergrok", Limit = "77" };
var merged = UsageRefreshLogic.Merge("supergrok", "SuperGrok", sgPresent, manual, DateTimeOffset.UnixEpoch);
Eq("merge-used", merged.Used, "12");
Eq("merge-limit", merged.Limit, "77");
if (!merged.Source.Contains("manual", StringComparison.Ordinal)) { fails += 1; Console.Error.WriteLine("FAIL merge-src"); }

Ok("interval-off", UsageRefreshLogic.IntervalOffByDefault(0));
Ok("interval-clamp", UsageRefreshLogic.ClampIntervalMinutes(2) == 5);
Ok("interval-min", !UsageRefreshLogic.ShouldRunInterval(0, false, DateTimeOffset.MinValue, DateTimeOffset.Now));
Ok("interval-minimized", !UsageRefreshLogic.ShouldRunInterval(5, true, DateTimeOffset.MinValue, DateTimeOffset.Now));

foreach (var id in UsageRefreshLogic.Sites.Keys)
    Ok("script-" + id, UsageRefreshLogic.ScriptIsReadOnly(UsageRefreshLogic.DomScript(id)));

Ok("hdr-ep-ok", UsageRefreshLogic.RateLimitEndpointOk("https://api.x.com/2/usage/tweets"));
Ok("hdr-ep-query", UsageRefreshLogic.RateLimitEndpointOk("https://api.x.com/2/usage/tweets?granularity=daily"));
Ok("hdr-ep-host", !UsageRefreshLogic.RateLimitEndpointOk("https://evil.example/2/usage/tweets"));
Ok("hdr-ep-suffix", !UsageRefreshLogic.RateLimitEndpointOk("https://api.x.com.evil.example/2/usage/tweets"));
Ok("hdr-ep-path", !UsageRefreshLogic.RateLimitEndpointOk("https://api.x.com/2/other"));
Ok("hdr-ep-prefix", !UsageRefreshLogic.RateLimitEndpointOk("https://api.x.com/2/usage/tweets/extra"));
Ok("hdr-ep-http", !UsageRefreshLogic.RateLimitEndpointOk("http://api.x.com/2/usage/tweets"));
Ok("hdr-ep-userinfo", !UsageRefreshLogic.RateLimitEndpointOk("https://user:pass@api.x.com/2/usage/tweets"));
Ok("hdr-ep-grok", !UsageRefreshLogic.RateLimitEndpointOk("https://grok.com/"));
Ok("hdr-ep-x", !UsageRefreshLogic.RateLimitEndpointOk("https://x.com/settings"));
Ok("hdr-ep-empty", !UsageRefreshLogic.RateLimitEndpointOk(""));
Ok("hdr-ep-443", UsageRefreshLogic.RateLimitEndpointOk("https://api.x.com:443/2/usage/tweets"));
Ok("hdr-ep-port", !UsageRefreshLogic.RateLimitEndpointOk("https://api.x.com:444/2/usage/tweets"));

Ok("log-clean", UsageRefreshLogic.LogLine("supergrok", "ok") == "usage parse supergrok ok");
Ok("log-secret", UsageRefreshLogic.LogLine("supergrok", "bearer abc") == "usage parse redacted");
Ok("log-url", UsageRefreshLogic.LooksSecret("https://x.com/settings"));

if (fails != 0)
{
    Console.Error.WriteLine("{0} mutant(s)", fails);
    return 1;
}
Console.WriteLine("usage-refresh-ok");
return 0;
