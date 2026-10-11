using Shell;

var fails = 0;
const string HubHost = "127.0.0.1:8107";

void Ok(string name, bool got, bool want)
{
    if (got == want) return;
    fails += 1;
    Console.Error.WriteLine("FAIL {0}: got {1} want {2}", name, got, want);
}

void Text(string name, string got, string want)
{
    if (got == want) return;
    fails += 1;
    Console.Error.WriteLine("FAIL {0}: got {1} want {2}", name, got, want);
}

Ok("grok", OpenExternalGuard.Allowed("https://grok.com/", HubHost), true);
Ok("x", OpenExternalGuard.Allowed("https://x.com/", HubHost), true);
Ok("folder", OpenExternalGuard.Allowed(@"C:\hbc\data", HubHost), true);
Ok("token-query", OpenExternalGuard.Allowed("https://grok.com/?token=abc", HubHost), false);
Ok("token-case", OpenExternalGuard.Allowed("https://grok.com/?TOKEN=abc", HubHost), false);
Ok("token-encoded", OpenExternalGuard.Allowed("https://grok.com/?token%3Dabc", HubHost), false);
Ok("token-double", OpenExternalGuard.Allowed("https://grok.com/?token%253Dabc", HubHost), false);
Ok("token-mixed", OpenExternalGuard.Allowed("https://grok.com/?%74oken%3Dabc", HubHost), false);
Ok("hub-token", OpenExternalGuard.Allowed("http://127.0.0.1:8107/build.html?token=abc", HubHost), false);
Ok("hub-plain", OpenExternalGuard.Allowed("http://127.0.0.1:8107/build.html", HubHost), false);
Ok("hub-root", OpenExternalGuard.Allowed("http://127.0.0.1:8107/", HubHost), false);
Ok("hub-encoded", OpenExternalGuard.Allowed("http://127.0.0.1%3A8107/build.html", HubHost), false);

var kids = WebPageLayout.Children("view", "bar", "state");
if (kids.Length != 3 || kids[0] != "view" || kids[1] != "bar" || kids[2] != "state")
{
    fails += 1;
    Console.Error.WriteLine("FAIL layout-children: {0}", string.Join(",", kids));
}
if (string.Join(",", WebPageLayout.Names) != "view,bar,state")
{
    fails += 1;
    Console.Error.WriteLine("FAIL layout-names: {0}", string.Join(",", WebPageLayout.Names));
}

Text("limit-null", UsageStore.DisplayOrBlank(null), "");
Text("limit-empty", UsageStore.DisplayOrBlank(""), "");
Text("limit-ws", UsageStore.DisplayOrBlank("  "), "");
Text("limit-default", UsageStore.DefaultBlank, "");

if (fails != 0)
{
    Console.Error.WriteLine("{0} mutant(s)", fails);
    return 1;
}
Console.WriteLine("design-helpers-ok");
return 0;
