using System.Globalization;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace Shell;

public partial class Form1 : Form
{
    private static readonly Color Paper = HbcTheme.Bg;
    private static readonly Color Panel = HbcTheme.Surface;
    private static readonly Color Ink = HbcTheme.Text;
    private static readonly Color Green = HbcTheme.Emerald;
    private readonly string root = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "..", "..", "..", ".."));
    internal string RootPath => root;
    private readonly string statePath;
    private FlowLayoutPanel titleBar = null!;
    private Panel tabBar = null!;
    private readonly List<Button> tabButtons = new();
    private readonly List<Control> pages = new();
    private int activeTab;
    private Label dot = null!;
    private Button hubToggle = null!;
    private CheckBox autoStart = null!;
    private CheckBox startWithWindows = null!;
    private readonly string settingsPath;
    private bool autoStartHub = false;
    private bool hubBusy;
    private bool hubTickBusy;
    private string? sessionTokenCache;
    private bool sessionTokenLoaded;
    private System.Windows.Forms.Timer hubTimer = null!;
    private Button? buildTab;
    private Button healthChip = null!;
    private Button usageChip = null!;
    private Button moreBtn = null!;
    private Label statusState = null!;
    private Label hubReady = null!;
    private DateTime popupClosedAt = DateTime.MinValue;
    private DateTime hubDownSince = DateTime.UtcNow;
    private bool hubSeenUp;
    private readonly Dictionary<string, TabStatePanel> tabStates = new(StringComparer.Ordinal);
    private readonly Dictionary<string, WebView2> tabViews = new(StringComparer.Ordinal);
    private readonly Dictionary<string, UsageRefreshLogic.HeaderSnap> usageHeaders = new(StringComparer.Ordinal);
    private readonly Dictionary<string, WebTab> webTabs = new(StringComparer.Ordinal);
    private int usageRefreshMinutes;
    private static readonly string[] TabOrder = { "SuperGrok", "X", "Imagine", "Build", "Control" };
    private static readonly int DefaultHubPort = 8107;
    private static readonly string HubBase = "http://127.0.0.1:" + ReadHubPort();

    // HBC_HUB_PORT is process-only. Never setx. ASCII [0-9] only.
    private static int ReadHubPort()
    {
        var raw = Environment.GetEnvironmentVariable("HBC_HUB_PORT");
        if (string.IsNullOrWhiteSpace(raw)) return DefaultHubPort;
        var trimmed = raw.Trim();
        if (!Regex.IsMatch(trimmed, "^[0-9]+$")
            || !int.TryParse(trimmed, NumberStyles.None, CultureInfo.InvariantCulture, out var port)
            || port < 1024 || port > 65535)
        {
            Program.Log("HBC_HUB_PORT refused; using " + DefaultHubPort);
            return DefaultHubPort;
        }
        return port;
    }

    public Form1()
    {
        statePath = Path.Combine(root, "window-state.txt");
        settingsPath = Path.Combine(root, "shell-settings.txt");
        Text = BrandAssets.ProductName;
        Width = 1280;
        Height = 860;
        MinimumSize = new Size(960, 600);
        BackColor = Paper;
        ForeColor = Ink;
        StartPosition = FormStartPosition.CenterScreen;
        RestoreWindow();
        BrandAssets.ApplyWindowIcon(this, root);
        var splash = BrandAssets.MakeSplash(root);
        splash.Show();

        UseDarkTitleBar();
        titleBar = new FlowLayoutPanel
        {
            Name = "TitleBar",
            Dock = DockStyle.Top,
            AutoSize = true,
            WrapContents = true,
            BackColor = Panel,
            Padding = new Padding(8, 4, 8, 4),
        };
        var mark = BrandAssets.TitleBarMark(root);
        var title = new Label { Text = BrandAssets.ProductName, AutoSize = true, ForeColor = HbcTheme.Gold, Font = HbcTheme.Ui(11f, FontStyle.Bold), Margin = new Padding(4, 8, 12, 4) };
        dot = new Label { Text = "hub", AutoSize = true, ForeColor = Ink, Margin = new Padding(8, 10, 4, 4) };
        hubToggle = new Button { Text = "Start", FlatStyle = FlatStyle.Flat, ForeColor = Ink, BackColor = HbcTheme.Surface2, AutoSize = true, Margin = new Padding(4) };
        hubToggle.Click += async (_, _) => await ToggleHub();
        var startAll = new Button { Text = "Start All", FlatStyle = FlatStyle.Flat, ForeColor = HbcTheme.OnAccent, BackColor = Green, AutoSize = true, Margin = new Padding(4) };
        startAll.Click += async (_, _) => await StartAll();
        autoStart = new CheckBox { Text = "Auto-start hub", ForeColor = Ink, BackColor = Panel, AutoSize = true, Margin = new Padding(8, 10, 4, 4) };
        startWithWindows = new CheckBox { Text = "Start with Windows", ForeColor = Ink, BackColor = Panel, AutoSize = true, Margin = new Padding(4, 10, 4, 4) };
        LoadSettings();
        if (!File.Exists(settingsPath)) SaveSettings();
        autoStart.Checked = autoStartHub;
        startWithWindows.Checked = StartupShortcutExists();
        autoStart.CheckedChanged += (_, _) => { autoStartHub = autoStart.Checked; SaveSettings(); };
        startWithWindows.CheckedChanged += (_, _) => SetStartup(startWithWindows.Checked);
        healthChip = MakeChip("All systems OK");
        usageChip = MakeChip("Usage");
        usageChip.Name = "UsagePopupButton";
        usageChip.AccessibleName = "UsagePopupButton";
        moreBtn = MakeChip("⋯");
        healthChip.Click += (_, _) => ToggleHealthPopup();
        usageChip.Click += (_, _) => ToggleUsagePopup();
        moreBtn.Click += (_, _) => ToggleMoreMenu();
        notice = new Label { Text = "", AutoSize = false, ForeColor = HbcTheme.Text2, BackColor = Panel, Dock = DockStyle.Bottom, Height = HbcTheme.StatusBarHeight };
        hubReady = new Label
        {
            Name = "HubStatus",
            AccessibleName = "HubStatus",
            Text = "hub not yet",
            AutoSize = false,
            Width = 180,
            Dock = DockStyle.Right,
            TextAlign = ContentAlignment.MiddleRight,
            ForeColor = HbcTheme.Text2,
            BackColor = Panel,
        };
        statusState = new Label { Text = "Ready · Data stays on this PC", AutoSize = false, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, ForeColor = HbcTheme.Text2, BackColor = Panel };
        notice.Controls.Add(hubReady);
        notice.Controls.Add(statusState);
        dot.Name = "HubDot";
        titleBar.Controls.Add(mark);
        titleBar.Controls.Add(title);
        titleBar.Controls.Add(dot);
        titleBar.Controls.Add(hubToggle);
        titleBar.Controls.Add(startAll);
        titleBar.Controls.Add(healthChip);
        titleBar.Controls.Add(usageChip);
        titleBar.Controls.Add(moreBtn);
        titleBar.Controls.Add(autoStart);
        titleBar.Controls.Add(startWithWindows);

        tabBar = new FlowLayoutPanel { Dock = DockStyle.Top, Height = HbcTheme.TabBarHeight, BackColor = Panel, Padding = new Padding(8, 6, 0, 0) };
        tabBar.Controls.Add(BrandAssets.TabIcon(root));
        Controls.Add(notice);
        Controls.Add(tabBar);
        Controls.Add(titleBar);

        tabBar.BringToFront();
        Resize += (_, _) =>
        {
            DismissPopups();
            if (WindowState == FormWindowState.Minimized) return;
            if (pages.Count > 0 && activeTab >= 0 && activeTab < pages.Count)
                SyncShownTab(pages[activeTab]);
        };
        ResizeBegin += (_, _) => DismissPopups();
        Move += (_, _) => DismissPopups();
        ResizeEnd += (_, _) => SaveWindow();
        FormClosing += (_, e) => { SaveWindow(); StopUiTimers(); Program.Log("closing " + e.CloseReason); };
        HandleCreated += (_, _) => UiWatchdog.Start(this);
        InitPopupDismiss();
        Load += async (_, _) =>
        {
            try
            {
                if (autoStartHub && !await HubUpProbe()) await StartHub();
                var (up, ms) = await WaitForHubAsync(5000);
                if (up) NoteHubConnected(ms);
                else NoteHubDown();
                await RefreshHub();
                hubTimer = new System.Windows.Forms.Timer { Interval = 5000 };
                hubTimer.Tick += OnHubTick;
                hubTimer.Start();
                await BuildTabs();
                Program.Log("tabs ready");
            }
            catch (Exception ex)
            {
                Program.Log("tabs " + ex);
            }
            finally
            {
                splash.Close();
                splash.Dispose();
            }
        };
    }

    private async void OnHubTick(object? sender, EventArgs e)
    {
        if (hubTickBusy) return;
        hubTickBusy = true;
        try { await RefreshHub(); }
        finally { hubTickBusy = false; }
    }

    private void StopUiTimers()
    {
        hubTimer?.Stop();
        healthTimer?.Stop();
        jobTimer?.Stop();
        feedDebounce?.Stop();
        usageTimer?.Stop();
        feedWatch?.Dispose();
    }

    internal static bool TokenRejected(System.Net.HttpStatusCode code) =>
        code == System.Net.HttpStatusCode.Unauthorized
        || code == System.Net.HttpStatusCode.Forbidden;

    internal string SessionToken(bool force = false)
    {
        if (force)
        {
            sessionTokenLoaded = false;
            sessionTokenCache = null;
        }
        if (sessionTokenLoaded) return sessionTokenCache ?? "";
        var tokenPath = Path.Combine(root, ".session-token");
        sessionTokenCache = File.Exists(tokenPath) ? File.ReadAllText(tokenPath).Trim() : "";
        sessionTokenLoaded = true;
        return sessionTokenCache;
    }

    internal Task<string> SessionTokenAsync(bool force = false)
    {
        if (!force && sessionTokenLoaded)
            return Task.FromResult(sessionTokenCache ?? "");
        return Task.Run(() => SessionToken(force));
    }

    internal void InvalidateSessionToken()
    {
        sessionTokenLoaded = false;
        sessionTokenCache = null;
    }

    private async Task RefreshHub()
    {
        if (IsDisposed) return;
        var up = await HubUp();
        dot.Text = up ? "hub  on" : "hub  off";
        dot.ForeColor = up ? Green : HbcTheme.Error;
        hubToggle.Text = up ? "Stop" : "Start";
        if (healthChip is not null && !healthChip.IsDisposed)
            healthChip.Text = up ? "All systems OK" : "1 thing needs a look";
        var wasDown = !hubSeenUp;
        if (up) NoteHubConnected(null);
        else NoteHubDown();
        if (!up)
        {
            SetBuildBadge(0);
            return;
        }
        if (wasDown) await RefreshBuildTokenAfterHubUp();
        await RefreshBuildBadge();
    }

    private static bool IsHubBuildPage(Uri? source)
    {
        if (source is null) return false;
        Uri hub;
        try
        {
            hub = new Uri(HubBase);
        }
        catch (UriFormatException)
        {
            return false;
        }
        return source.Scheme == Uri.UriSchemeHttp
            && source.Host == "127.0.0.1"
            && source.Port == hub.Port
            && string.Equals(source.AbsolutePath, "/build.html", StringComparison.Ordinal);
    }

    private async Task RefreshBuildTokenAfterHubUp()
    {
        var token = await SessionTokenAsync(force: true);
        if (token.Length == 0) return;
        var next = HubBase + "/build.html?token=" + Uri.EscapeDataString(token);
        buildUrl = next;
        if (webTabs.TryGetValue("Build", out var tab)) tab.Url = next;
        try
        {
            if (buildView is null || buildView.IsDisposed) return;
            var core = buildView.CoreWebView2;
            if (core is null) return;
            if (!IsHubBuildPage(buildView.Source)) return;
            if (!Uri.TryCreate(core.Source, UriKind.Absolute, out var live) || !IsHubBuildPage(live)) return;
            await core.ExecuteScriptAsync("window.__hbcToken = " + JsonSerializer.Serialize(token) + ";");
        }
        catch (Exception)
        {
        }
    }

    private Task RefreshBuildBadge()
    {
        SetBuildBadge(0);
        return Task.CompletedTask;
    }

    private void SetBuildBadge(int waiting)
    {
        if (buildTab is null || buildTab.IsDisposed) return;
        buildTab.Text = "Build";
    }

    private async Task ToggleHub()
    {
        if (hubBusy) return;
        hubBusy = true;
        try
        {
            if (await HubUp()) StopHub();
            else await StartHub();
            await RefreshHub();
        }
        catch (Exception ex)
        {
            Program.Log("hub toggle " + ex.Message);
        }
        finally
        {
            hubBusy = false;
        }
    }

    private static void ApplyHubSpawnEnv(System.Diagnostics.ProcessStartInfo psi)
    {
        var staging = Environment.GetEnvironmentVariable("HBC_STAGING");
        if (staging != "1") return;
        psi.EnvironmentVariables["HBC_STAGING"] = "1";
        var port = Environment.GetEnvironmentVariable("HBC_HUB_PORT");
        if (!string.IsNullOrWhiteSpace(port))
            psi.EnvironmentVariables["HBC_HUB_PORT"] = port;
    }

    private async Task StartHub()
    {
        var starter = Path.Combine(root, "Start-HiveBrainComms.ps1");
        var psi = new System.Diagnostics.ProcessStartInfo
        {
            FileName = "powershell.exe",
            Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"" + starter + "\"",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = System.Diagnostics.ProcessWindowStyle.Hidden,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        ApplyHubSpawnEnv(psi);
        using var proc = System.Diagnostics.Process.Start(psi);
        if (proc is not null)
        {
            var readOut = proc.StandardOutput.ReadToEndAsync();
            var readErr = proc.StandardError.ReadToEndAsync();
            await Task.WhenAny(proc.WaitForExitAsync(), Task.Delay(6000));
            if (proc.HasExited && proc.ExitCode != 0)
            {
                var output = ((await readOut) + "\n" + (await readErr)).Trim();
                Program.Log("hub start refused " + output);
                Say(output.Length > 0 ? output : "Hub refused to start.");
                return;
            }
        }
        var (up, ms) = await WaitForHubAsync(8000);
        if (up) NoteHubConnected(ms);
        await SessionTokenAsync(force: true);
        await RefreshBuildTokenAfterHubUp();
    }

    private static async Task<HttpResponseMessage> SendHubState(HttpClient client, string token)
    {
        using var request = new HttpRequestMessage(HttpMethod.Get, HubBase + "/api/state");
        request.Headers.TryAddWithoutValidation("X-Hbc-Token", token);
        request.Headers.Host = new Uri(HubBase).Authority;
        return await client.SendAsync(request);
    }

    internal Task StartHubPublic() => StartHub();

    private void StopHub()
    {
        var path = Path.Combine(root, ".hub-process");
        if (!File.Exists(path))
        {
            Say("Hub process record is missing. Not stopping.");
            return;
        }
        try
        {
            var started = ReadStartedProcess(path);
            if (started is null)
            {
                Say("Hub process record is corrupt. Not stopping.");
                return;
            }
            System.Diagnostics.Process proc;
            try
            {
                proc = System.Diagnostics.Process.GetProcessById(started.Pid);
            }
            catch (ArgumentException)
            {
                Say("Hub process record is stale. Not stopping.");
                return;
            }
            using (proc)
            {
                if (!HubIdentityMatches(proc, root, started.Ctime))
                {
                    Say("Hub process record is stale. Not stopping.");
                    return;
                }
                proc.Kill();
                try { File.Delete(path); } catch (IOException) { }
                Program.Log("hub stopped " + proc.Id);
            }
        }
        catch (Exception ex)
        {
            Say("Hub process record could not be read. Not stopping. " + ex.Message);
            Program.Log("hub stop " + ex.Message);
        }
    }

    private sealed class StartedProcess
    {
        public int Pid;
        public long Ctime;
    }

    private static StartedProcess? ReadStartedProcess(string path)
    {
        if (!File.Exists(path)) return null;
        var pid = 0;
        long ctime = 0;
        foreach (var raw in File.ReadAllLines(path))
        {
            var line = raw.Trim();
            if (line.StartsWith("pid=", StringComparison.OrdinalIgnoreCase))
                int.TryParse(line.Substring(4), out pid);
            else if (line.StartsWith("ctime=", StringComparison.OrdinalIgnoreCase))
                long.TryParse(line.Substring(6), out ctime);
        }
        if (pid <= 0 || ctime == 0) return null;
        return new StartedProcess { Pid = pid, Ctime = ctime };
    }

    private Label notice = null!;

    private void Say(string text)
    {
        notice.Text = text;
        SetStatus(text + " · Data stays on this PC");
        Program.Log("notice " + text);
    }

    private async Task<bool> HubIsStaging()
    {
        try
        {
            var token = await SessionTokenAsync();
            if (token.Length == 0) token = await SessionTokenAsync(force: true);
            if (token.Length == 0) return false;
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
            using var response = await SendHubState(client, token);
            if (!response.IsSuccessStatusCode) return false;
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            return doc.RootElement.TryGetProperty("staging", out var el) && el.ValueKind == JsonValueKind.True;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private async Task StartAll()
    {
        try
        {
            if (!await HubUp())
            {
                await StartHub();
                if (!await HubUp()) Say("Hub is down. Start did not get a 200 from " + HubBase + ".");
            }
            await RefreshHub();
            if (await HubIsStaging())
            {
                SelectTab(IndexOf("Build"));
                Say("Staging hub: Build watcher is off.");
                FocusOrStartBot();
                Program.Log("start all staging");
                return;
            }
            if (await Task.Run(() => BuildSessionOpen()))
            {
                await Task.Run(() => FocusBuild());
                Say("Build is already open. Focused it.");
            }
            else
            {
                SelectTab(IndexOf("Build"));
                Say("Build tab is selected. Sign in there if the resume prompt is waiting.");
            }
            FocusOrStartBot();
            Program.Log("start all");
        }
        catch (Exception ex)
        {
            Say("Start All failed. " + ex.Message);
        }
    }

    private int IndexOf(string title)
    {
        for (var i = 0; i < tabButtons.Count; i++)
            if ((tabButtons[i].Tag as string) == title) return i;
        return 0;
    }

    private static bool BuildSessionOpen()
    {
        foreach (var proc in System.Diagnostics.Process.GetProcessesByName("WindowsTerminal"))
        {
            using (proc)
            {
                var command = CommandLineOf(proc.Id);
                if (command.Contains("--title \"Grok Build\"", StringComparison.Ordinal) && proc.MainWindowHandle != IntPtr.Zero)
                    return true;
            }
        }
        foreach (var proc in System.Diagnostics.Process.GetProcessesByName("grok"))
        {
            using (proc)
            {
                var command = CommandLineOf(proc.Id);
                if (command.Contains("--continue", StringComparison.Ordinal) || command.Contains("--cwd", StringComparison.Ordinal))
                    return true;
            }
        }
        return false;
    }

    private static void FocusBuild()
    {
        foreach (var proc in System.Diagnostics.Process.GetProcessesByName("WindowsTerminal"))
        {
            using (proc)
            {
                var command = CommandLineOf(proc.Id);
                if (!command.Contains("--title \"Grok Build\"", StringComparison.Ordinal)) continue;
                if (proc.MainWindowHandle == IntPtr.Zero) continue;
                ShowWindow(proc.MainWindowHandle, 9);
                SetForegroundWindow(proc.MainWindowHandle);
                return;
            }
        }
    }

    private static void FocusOrStartBuild(string root)
    {
        foreach (var proc in System.Diagnostics.Process.GetProcessesByName("WindowsTerminal"))
        {
            using (proc)
            {
                try
                {
                    var command = CommandLineOf(proc.Id);
                    if (!command.Contains("--title \"Grok Build\"", StringComparison.Ordinal)) continue;
                    if (proc.MainWindowHandle == IntPtr.Zero) continue;
                    ShowWindow(proc.MainWindowHandle, 9);
                    SetForegroundWindow(proc.MainWindowHandle);
                    return;
                }
                catch (Exception ex)
                {
                    Program.Log("build focus " + ex.Message);
                }
            }
        }
        var link = FindBuildLink(root);
        if (link is not null)
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(link) { UseShellExecute = true });
        else
            Program.Log("Grok Build shortcut was not found beside the app or on the Desktop.");
    }

    private static string? FindBuildLink(string root)
    {
        var local = Path.Combine(root, "Grok Build.lnk");
        if (File.Exists(local)) return local;
        var desks = new[]
        {
            Environment.GetFolderPath(Environment.SpecialFolder.Desktop),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), "OneDrive", "Desktop"),
        };
        foreach (var desk in desks)
        {
            if (string.IsNullOrEmpty(desk)) continue;
            var link = Path.Combine(desk, "Grok Build.lnk");
            if (File.Exists(link)) return link;
        }
        return null;
    }

    private static bool HubIdentityMatches(System.Diagnostics.Process proc, string rootPath, long ctime)
    {
        if (proc.StartTime.ToFileTimeUtc() != ctime) return false;
        if (!IsPythonImage(proc)) return false;
        var argv = SplitCommandLine(CommandLineOf(proc.Id));
        if (argv.Count < 2 || !argv[1].Equals("server.py", StringComparison.Ordinal)) return false;
        var cwd = ProcessCwd(proc.Id);
        if (string.IsNullOrEmpty(cwd)) return false;
        var rootFull = Path.GetFullPath(rootPath);
        var cwdFull = Path.GetFullPath(cwd);
        var scriptFull = Path.GetFullPath(Path.Combine(cwdFull, argv[1]));
        var rootScript = Path.GetFullPath(Path.Combine(rootFull, "server.py"));
        return PathsEqual(cwdFull, rootFull) || PathsEqual(scriptFull, rootScript);
    }

    // Microsoft Store Python ships as python3.12.exe / pythonw3.12.exe.
    // Exact image: python, optional w, optional version (3 or 3.12), then .exe.
    private static readonly Regex PythonImageName = new Regex(
        @"^pythonw?(?:\d+(?:\.\d+)*)?\.exe$",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    private static bool IsPythonImage(System.Diagnostics.Process proc)
    {
        try
        {
            var image = Path.GetFileName(proc.MainModule?.FileName ?? "");
            return PythonImageName.IsMatch(image);
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static bool PathsEqual(string left, string right)
    {
        var a = Path.GetFullPath(left).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        var b = Path.GetFullPath(right).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        return string.Equals(a, b, StringComparison.OrdinalIgnoreCase);
    }

    private static List<string> SplitCommandLine(string command)
    {
        var args = new List<string>();
        if (string.IsNullOrWhiteSpace(command)) return args;
        var cur = new System.Text.StringBuilder();
        var quote = false;
        foreach (var ch in command)
        {
            if (ch == '"')
            {
                quote = !quote;
                continue;
            }
            if (char.IsWhiteSpace(ch) && !quote)
            {
                if (cur.Length > 0)
                {
                    args.Add(cur.ToString());
                    cur.Clear();
                }
                continue;
            }
            cur.Append(ch);
        }
        if (cur.Length > 0) args.Add(cur.ToString());
        return args;
    }

    private static string CommandLineOf(int pid) => PebRead.CommandLine(pid);

    private static string ProcessCwd(int pid) => PebRead.CurrentDirectory(pid);

    private static void FocusOrStartBot()
    {
        foreach (var proc in System.Diagnostics.Process.GetProcessesByName("Grok Bot"))
        {
            using (proc)
            {
                if (proc.MainWindowHandle == IntPtr.Zero) continue;
                ShowWindow(proc.MainWindowHandle, 9);
                SetForegroundWindow(proc.MainWindowHandle);
                return;
            }
        }
        var exe = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Programs", "Grok Bot", "Grok Bot.exe");
        if (!File.Exists(exe))
        {
            Program.Log("Grok Bot.exe was not found");
            return;
        }
        var started = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(exe)
        {
            UseShellExecute = true,
            WindowStyle = System.Diagnostics.ProcessWindowStyle.Minimized,
        });
        if (started is not null) ShowWindow(started.MainWindowHandle, 6);
    }

    private void LoadSettings()
    {
        autoStartHub = false;
        usageRefreshMinutes = 0;
        if (!File.Exists(settingsPath)) return;
        foreach (var line in File.ReadAllLines(settingsPath))
        {
            if (line.StartsWith("auto-start-hub=", StringComparison.Ordinal))
                autoStartHub = line.EndsWith("on", StringComparison.Ordinal);
            if (line.StartsWith("usage-refresh-minutes=", StringComparison.Ordinal))
            {
                var raw = line.Substring("usage-refresh-minutes=".Length).Trim();
                if (int.TryParse(raw, NumberStyles.None, CultureInfo.InvariantCulture, out var minutes))
                    usageRefreshMinutes = UsageRefreshLogic.ClampIntervalMinutes(minutes);
            }
        }
    }

    private void SaveSettings()
    {
        File.WriteAllText(
            settingsPath,
            "auto-start-hub=" + (autoStartHub ? "on" : "off") + Environment.NewLine
            + "usage-refresh-minutes=" + usageRefreshMinutes.ToString(CultureInfo.InvariantCulture) + Environment.NewLine);
    }

    private static string StartupLink => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "HiveBrainComms.lnk");

    private static bool StartupShortcutExists() => File.Exists(StartupLink);

    private void SetStartup(bool enabled)
    {
        if (enabled)
        {
            var target = Path.Combine(root, "Open-HiveBrainComms-Shell.cmd");
            var script = "$s = New-Object -ComObject WScript.Shell; $l = $s.CreateShortcut('" + StartupLink + "'); $l.TargetPath = '" + target + "'; $l.WorkingDirectory = '" + root + "'; $l.Save()";
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = "-NoProfile -Command \"" + script + "\"",
                UseShellExecute = false,
                CreateNoWindow = true,
            });
        }
        else if (File.Exists(StartupLink))
        {
            File.Delete(StartupLink);
        }
        Program.Log("startup " + (enabled ? "on" : "off"));
    }

    private static IntPtr FindWindowByTitle(string title)
    {
        var found = IntPtr.Zero;
        EnumWindows((handle, _) =>
        {
            var buffer = new System.Text.StringBuilder(512);
            GetWindowText(handle, buffer, buffer.Capacity);
            if (buffer.ToString().Contains(title, StringComparison.Ordinal))
            {
                found = handle;
                return false;
            }
            return true;
        }, IntPtr.Zero);
        return found;
    }

    private async Task BuildTabs()
    {
        await AddWeb("SuperGrok", "https://grok.com/", "supergrok");
        await AddWeb("X", "https://x.com/", "x");
        await AddWeb("Imagine", "https://grok.com/imagine", "imagine");
        await AddBuild();
        AddControl();
        RestoreTab();
    }

    internal static async Task<bool> HubUp()
    {
        return await HubReachable(TimeSpan.FromSeconds(2));
    }

    internal static async Task<bool> HubUpProbe()
    {
        return await HubReachable(TimeSpan.FromMilliseconds(400));
    }

    private static async Task<bool> HubReachable(TimeSpan timeout)
    {
        try
        {
            using var client = new HttpClient { Timeout = timeout };
            var response = await client.GetAsync(HubBase + "/");
            return response.IsSuccessStatusCode;
        }
        catch (Exception)
        {
            return false;
        }
    }

    internal static async Task<(bool up, int ms)> WaitForHubAsync(int budgetMs)
    {
        var started = DateTime.UtcNow;
        var delay = 150;
        while (true)
        {
            if (await HubUpProbe())
            {
                var ms = Math.Max(0, (int)(DateTime.UtcNow - started).TotalMilliseconds);
                return (true, ms);
            }
            var elapsed = (int)(DateTime.UtcNow - started).TotalMilliseconds;
            if (elapsed >= budgetMs)
                return (false, elapsed);
            await Task.Delay(Math.Min(delay, Math.Max(1, budgetMs - elapsed)));
            delay = Math.Min(delay * 2, 800);
        }
    }

    private void NoteHubConnected(int? ms)
    {
        var elapsed = ms ?? Math.Max(0, (int)(DateTime.UtcNow - hubDownSince).TotalMilliseconds);
        if (hubSeenUp) return;
        hubSeenUp = true;
        Program.Log("hub connected " + elapsed);
        if (hubReady is not null && !hubReady.IsDisposed)
            hubReady.Text = "hub connected " + elapsed;
    }

    private void NoteHubDown()
    {
        if (hubSeenUp) hubDownSince = DateTime.UtcNow;
        hubSeenUp = false;
        if (hubReady is not null && !hubReady.IsDisposed)
            hubReady.Text = "hub not yet";
    }

    private void RestoreWindow()
    {
        if (!File.Exists(statePath)) return;
        var parts = File.ReadAllText(statePath).Split(',');
        if (parts.Length < 4 || !int.TryParse(parts[0], out var x)) return;
        int.TryParse(parts[1], out var y);
        int.TryParse(parts[2], out var w);
        int.TryParse(parts[3], out var h);
        if (w > 400 && h > 300) { StartPosition = FormStartPosition.Manual; Bounds = new Rectangle(x, y, w, h); }
    }

    private void RestoreTab()
    {
        var fallback = IndexOf("SuperGrok");
        if (fallback < 0) fallback = IndexOf("Build");
        if (!File.Exists(statePath))
        {
            SelectTab(fallback);
            return;
        }
        var parts = File.ReadAllText(statePath).Split(',');
        if (parts.Length > 4 && RemovedTabName(parts[4]))
        {
            SelectTab(fallback);
            return;
        }
        if (parts.Length > 4 && int.TryParse(parts[4], out var index) && index >= 0 && index < pages.Count)
            SelectTab(index);
        else
            SelectTab(fallback);
    }

    private static bool RemovedTabName(string raw)
    {
        var name = raw.Trim();
        return name.Equals("Hub", StringComparison.OrdinalIgnoreCase)
            || name.Equals("Grok Bot", StringComparison.OrdinalIgnoreCase)
            || name.Equals("Freebots", StringComparison.OrdinalIgnoreCase)
            || name.Equals("FreeBots", StringComparison.OrdinalIgnoreCase);
    }

    private void SaveWindow()
    {
        if (WindowState != FormWindowState.Normal) return;
        var b = Bounds;
        File.WriteAllText(statePath, $"{b.X},{b.Y},{b.Width},{b.Height},{activeTab}");
    }

    private void UseDarkTitleBar()
    {
        var preference = 1;
        DwmSetWindowAttribute(Handle, 20, ref preference, sizeof(int));
    }

    private Button MakeTab(string title)
    {
        var button = new Button
        {
            Text = title,
            Tag = title,
            FlatStyle = FlatStyle.Flat,
            ForeColor = HbcTheme.Text2,
            BackColor = Panel,
            AutoSize = true,
            Height = 32,
            Margin = new Padding(4, 0, 4, 0),
            Font = HbcTheme.Ui(10f, FontStyle.Bold),
        };
        button.FlatAppearance.BorderSize = 0;
        var index = tabButtons.Count;
        button.Click += (_, _) => SelectTab(index);
        tabButtons.Add(button);
        tabBar.Controls.Add(button);
        return button;
    }

    private void SelectTab(int index)
    {
        DismissPopups();
        activeTab = index;
        for (var i = 0; i < pages.Count; i++) pages[i].Visible = i == index;
        SyncShownTab(pages[index]);
        foreach (var button in tabButtons)
        {
            button.FlatAppearance.BorderSize = 0;
            button.ForeColor = HbcTheme.Text2;
            button.BackColor = Panel;
        }
        tabButtons[index].FlatAppearance.BorderColor = Green;
        tabButtons[index].FlatAppearance.BorderSize = 2;
        tabButtons[index].ForeColor = Green;
        tabButtons[index].BackColor = Paper;
        var title = tabButtons[index].Tag as string ?? "";
        SetStatus(title + " ready · Data stays on this PC");
        SaveWindow();
    }

    private void SyncShownTab(Control page)
    {
        SyncWebViews(page);
        foreach (Control child in page.Controls)
        {
            if (child is TabStatePanel state && state.Visible)
                state.BringToFront();
        }
    }

    // Hidden Dock=Fill panels are 0x0 when EnsureCoreWebView2Async runs.
    // WebView2 runtime 154 then keeps the compositor HWND at 0. Create each
    // view at an explicit tab-host Size/Bounds before Ensure. On show, a
    // Dock toggle that lands on the same Size is a no-op, so nudge 1px
    // then restore and notify the controller.
    private Size TabHostClientSize()
    {
        var bottom = notice is null ? HbcTheme.StatusBarHeight : notice.Height;
        var top = titleBar is null || titleBar.Height < HbcTheme.TitleBarHeight
            ? HbcTheme.TitleBarHeight + HbcTheme.TabBarHeight
            : titleBar.Height + HbcTheme.TabBarHeight;
        return WebViewHostBounds.TabHostSize(ClientSize, top, bottom);
    }

    private void PrepareWebView(WebView2 view)
    {
        var size = TabHostClientSize();
        view.Dock = DockStyle.None;
        view.Size = size;
        view.Bounds = new Rectangle(Point.Empty, size);
    }

    private static int TopReserve(Control page)
    {
        var top = 0;
        foreach (Control child in page.Controls)
        {
            if (child is WebView2 || !child.Visible) continue;
            if (child.Dock == DockStyle.Top) top += child.Height;
        }
        return top;
    }

    private static void SyncWebViews(Control page)
    {
        if (page is null || !page.Visible) return;
        page.PerformLayout();
        var target = WebViewHostBounds.FillBounds(page.ClientSize, TopReserve(page));
        foreach (Control child in page.Controls)
        {
            if (child is not WebView2 view) continue;
            var dock = view.Dock;
            view.Dock = DockStyle.None;
            var nudged = WebViewHostBounds.NudgeIfUnchanged(view.Size, target.Size);
            if (nudged != target.Size)
                view.Size = nudged;
            view.Size = target.Size;
            view.Bounds = target;
            NotifyWebViewController(view, target);
            view.Dock = dock;
        }
    }

    private static void NotifyWebViewController(WebView2 view, Rectangle bounds)
    {
        if (view.CoreWebView2 is null) return;
        try
        {
            const BindingFlags flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
            object? controller = null;
            foreach (var name in new[] { "Controller", "CoreWebView2Controller" })
            {
                controller = view.GetType().GetProperty(name, flags)?.GetValue(view)
                    ?? view.GetType().GetField(name, flags)?.GetValue(view);
                if (controller != null) break;
            }
            if (controller is null) return;
            controller.GetType().GetProperty("Bounds")?.SetValue(controller, bounds);
            controller.GetType().GetMethod("NotifyParentWindowPositionChanged", Type.EmptyTypes)?.Invoke(controller, null);
        }
        catch (Exception)
        {
        }
    }

    private async Task AddWeb(string title, string url, string profile)
    {
        MakeTab(title);
        var host = TabHostClientSize();
        var view = new WebView2 { Dock = DockStyle.None, Size = host, Bounds = new Rectangle(Point.Empty, host) };
        view.DefaultBackgroundColor = HbcTheme.Bg;
        var page = new Panel { Dock = DockStyle.Fill, BackColor = Paper, Visible = false };
        var bar = MakePageBar(title, view, url);
        var state = MakeTabState(title, view, url);
        foreach (var child in WebPageLayout.Children<Control>(view, bar, state))
            page.Controls.Add(child);
        pages.Add(page);
        Controls.Add(page);
        page.BringToFront();
        page.SizeChanged += (_, _) => SyncShownTab(page);
        var tab = new WebTab
        {
            Title = title,
            Url = url,
            Profile = profile,
            Page = page,
            View = view,
            State = state,
        };
        webTabs[title] = tab;
        try
        {
            var folder = Path.Combine(root, "profiles", profile);
            Directory.CreateDirectory(folder);
            var env = await Microsoft.Web.WebView2.Core.CoreWebView2Environment.CreateAsync(null, folder);
            tab.Env = env;
            PrepareWebView(view);
            await view.EnsureCoreWebView2Async(env);
            // #13: AttachOwnedWebView(view) after Ensure, never before Prepare.
            await AttachOwnedWebView(view);
            HookWebTab(tab);
            view.Source = new Uri(url);
            Program.Log("loaded " + title + " profile " + profile);
        }
        catch (Exception ex)
        {
            Program.Log("panel " + title + " " + ex.Message);
            state.ShowError(ex.Message);
            Say(title + " did not load. " + ex.Message);
        }
    }

    private async Task AddBuild()
    {
        buildTab = MakeTab("Build");
        var host = TabHostClientSize();
        var view = new WebView2 { Dock = DockStyle.None, Size = host, Bounds = new Rectangle(Point.Empty, host) };
        view.DefaultBackgroundColor = HbcTheme.Bg;
        var page = new Panel { Dock = DockStyle.Fill, BackColor = Paper, Visible = false };
        var banner = new Label
        {
            Text = "Build is not watching yet. This tab starts when you select it.",
            Dock = DockStyle.Top,
            Height = 24,
            ForeColor = Ink,
            BackColor = Panel,
        };
        var token = await SessionTokenAsync();
        if (token.Length == 0) token = await SessionTokenAsync(force: true);
        if (token.Length == 0) Say("Hub token is missing, so the Build tab cannot open the terminal.");
        buildUrl = HubBase + "/build.html?token=" + Uri.EscapeDataString(token);
        var state = MakeTabState("Build", view, buildUrl, allowOpenBrowser: false);
        foreach (var child in WebPageLayout.Children<Control>(view, banner, state))
            page.Controls.Add(child);
        state.BringToFront();
        pages.Add(page);
        Controls.Add(page);
        page.BringToFront();
        page.SizeChanged += (_, _) => SyncShownTab(page);
        var tab = new WebTab
        {
            Title = "Build",
            Url = buildUrl,
            Profile = "build",
            Page = page,
            View = view,
            State = state,
            IsBuild = true,
            Banner = banner,
        };
        webTabs["Build"] = tab;
        try
        {
            var folder = Path.Combine(root, "profiles", "build");
            Directory.CreateDirectory(folder);
            var env = await CoreWebView2Environment.CreateAsync(null, folder);
            tab.Env = env;
            PrepareWebView(view);
            await view.EnsureCoreWebView2Async(env);
            // #13: AttachOwnedWebView(view) after Ensure, never before Prepare.
            await AttachOwnedWebView(view);
            buildView = view;
            HookWebTab(tab);
            view.Source = new Uri(buildUrl);
        }
        catch (Exception ex)
        {
            state.ShowError(ex.Message);
            Say("Build tab did not load. " + ex.Message);
        }
    }

    private async Task OpenChild(string title, CoreWebView2Environment env, CoreWebView2NewWindowRequestedEventArgs e)
    {
        var allowed = new[] { "grok.com", "x.com", "accounts.x.ai" };
        var host = Uri.TryCreate(e.Uri, UriKind.Absolute, out var target) ? target.Host : "";
        var ok = allowed.Any(name => host == name || host.EndsWith("." + name, StringComparison.OrdinalIgnoreCase));
        if (!ok && target is not null && host == "127.0.0.1" && target.Port == new Uri(HubBase).Port)
            ok = true;
        if (!ok && host.Length > 0)
        {
            e.Handled = true;
            Program.Log("popup blocked " + host);
            return;
        }
        var child = new Form
        {
            Text = title + " sign-in",
            Width = 900,
            Height = 720,
            StartPosition = FormStartPosition.CenterParent,
            Owner = this,
            TopMost = false,
            ShowInTaskbar = false,
            KeyPreview = true,
        };
        if (PopupDismiss.ShouldBindChildFormEscape)
            child.KeyDown += (_, ev) => { if (PopupDismiss.ShouldHandleEscape((int)ev.KeyCode)) child.Close(); };
        var popup = new WebView2 { Dock = DockStyle.Fill };
        child.Controls.Add(popup);
        child.Show(this);
        await popup.EnsureCoreWebView2Async(env);
        await AttachOwnedWebView(popup);
        e.NewWindow = popup.CoreWebView2;
        popup.CoreWebView2.WindowCloseRequested += (_, _) => child.Close();
        Program.Log("popup for " + title);
    }

    private void ShowRetry(Panel page, WebView2 view, string url)
    {
        if (page.Controls.OfType<Panel>().Any(panel => panel.Tag as string == "retry")) return;
        var bar = new Panel { Dock = DockStyle.Top, Height = 36, BackColor = Green, Tag = "retry" };
        var label = new Label { Text = "Signed in to X. Click Retry to return.", ForeColor = Paper, AutoSize = true, Location = new Point(8, 8) };
        var retry = new Button { Text = "Retry sign-in", Left = 280, Top = 4, FlatStyle = FlatStyle.Flat };
        retry.Click += (_, _) => { page.Controls.Remove(bar); view.Source = new Uri(url); };
        bar.Controls.Add(label);
        bar.Controls.Add(retry);
        page.Controls.Add(bar);
        Program.Log("retry bar shown");
    }

    private delegate bool EnumProc(IntPtr handle, IntPtr param);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumProc callback, IntPtr param);

    [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
    private static extern int GetWindowText(IntPtr handle, System.Text.StringBuilder text, int count);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr handle);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr handle, int command);

    [System.Runtime.InteropServices.DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

    private void ShowAbout()
    {
        MessageBox.Show(
            BrandAssets.ProductName + "\r\nIcons and mark: original compass-rose outline. Fonts: system / Cascadia (OFL).\r\nData stays on this PC.",
            "About",
            MessageBoxButtons.OK);
    }

    private Button MakeChip(string text)
    {
        var chip = new Button
        {
            Text = text,
            FlatStyle = FlatStyle.Flat,
            ForeColor = Ink,
            BackColor = HbcTheme.Surface2,
            AutoSize = true,
            Height = 28,
            Margin = new Padding(4),
        };
        chip.FlatAppearance.BorderColor = HbcTheme.BorderStrong;
        chip.FlatAppearance.BorderSize = 1;
        return chip;
    }

    private Panel MakePageBar(string title, WebView2 view, string url)
    {
        var bar = new Panel { Dock = DockStyle.Top, Height = HbcTheme.ToolbarHeight, BackColor = HbcTheme.Surface2 };
        var back = IconButton("Back", 8);
        var forward = IconButton("Forward", 44);
        var reload = IconButton("Reload", 80);
        var site = new Label { Text = title, AutoSize = true, ForeColor = HbcTheme.Text2, Location = new Point(120, 10) };
        var open = IconButton("Open", 280);
        open.Text = "Open in browser";
        open.AutoSize = true;
        back.Click += (_, _) => { if (view.CoreWebView2?.CanGoBack == true) view.CoreWebView2.GoBack(); };
        forward.Click += (_, _) => { if (view.CoreWebView2?.CanGoForward == true) view.CoreWebView2.GoForward(); };
        reload.Click += (_, _) => { if (view.CoreWebView2 is not null) view.Reload(); };
        open.Click += (_, _) => OpenExternal(url);
        bar.Controls.Add(back);
        bar.Controls.Add(forward);
        bar.Controls.Add(reload);
        bar.Controls.Add(site);
        bar.Controls.Add(open);
        return bar;
    }

    private static Button IconButton(string text, int left) => new()
    {
        Text = text,
        Left = left,
        Top = 4,
        Width = 32,
        Height = 26,
        FlatStyle = FlatStyle.Flat,
        ForeColor = HbcTheme.Text,
        BackColor = HbcTheme.Surface3,
    };

    private TabStatePanel MakeTabState(string title, WebView2 view, string retryUrl, bool allowOpenBrowser = true)
    {
        var state = new TabStatePanel();
        tabStates[title] = state;
        if (!allowOpenBrowser)
            state.HideOpenBrowser();
        state.Retry += async (_, _) =>
        {
            if (webTabs.TryGetValue(title, out var tab))
            {
                await RestartWebTab(tab);
                return;
            }
            state.ShowLoading(title);
            if (view.CoreWebView2 is not null) view.Source = new Uri(retryUrl);
        };
        if (allowOpenBrowser)
            state.OpenBrowser += (_, _) => OpenExternal(retryUrl);
        return state;
    }

    private static void OpenExternal(string url)
    {
        var host = "";
        try { host = new Uri(HubBase).Authority; }
        catch (UriFormatException) { host = ""; }
        if (!OpenExternalGuard.Allowed(url, host))
        {
            Program.Log("open browser refused");
            return;
        }
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            Program.Log("open browser " + ex.Message);
        }
    }

    private void SetStatus(string text)
    {
        if (statusState is null || statusState.IsDisposed) return;
        statusState.Text = text;
    }

    private bool IgnoreChipReopen() => (DateTime.UtcNow - popupClosedAt).TotalMilliseconds < 200;

    private void MarkPopupClosed() => popupClosedAt = DateTime.UtcNow;

    private void ToggleHealthPopup()
    {
        if (IgnoreChipReopen()) return;
        if (DismissPopups()) { MarkPopupClosed(); return; }
        var menu = NewOwnedMenu();
        menu.Items.Add("Health");
        menu.Items.Add(healthChip.Text);
        menu.Items.Add("Open Control tab").Click += (_, _) => SelectTab(IndexOf("Control"));
        menu.Closed += (_, _) => MarkPopupClosed();
        menu.Show(healthChip, new Point(0, healthChip.Height));
    }

    private void ToggleUsagePopup()
    {
        if (IgnoreChipReopen()) return;
        if (DismissPopups()) { MarkPopupClosed(); return; }
        var menu = NewOwnedMenu();
        menu.Name = "UsagePopup";
        menu.AccessibleName = "UsagePopup";
        menu.Items.Add("Usage — type limits on Control");
        menu.Items.Add("Open Control tab").Click += (_, _) => SelectTab(IndexOf("Control"));
        menu.Closed += (_, _) => MarkPopupClosed();
        menu.Show(usageChip, new Point(0, usageChip.Height));
    }

    private void ToggleMoreMenu()
    {
        if (IgnoreChipReopen()) return;
        if (DismissPopups()) { MarkPopupClosed(); return; }
        var menu = NewOwnedMenu();
        menu.Items.Add("Settings").Click += (_, _) => SelectTab(IndexOf("Control"));
        menu.Items.Add("Check health now").Click += async (_, _) => await RefreshHub();
        menu.Items.Add("Usage details").Click += (_, _) => SelectTab(IndexOf("Control"));
        menu.Items.Add("About").Click += (_, _) => ShowAbout();
        menu.Items.Add("Open data folder").Click += (_, _) => OpenExternal(root);
        menu.Items.Add("Quit").Click += (_, _) => Close();
        menu.Closed += (_, _) => MarkPopupClosed();
        menu.Show(moreBtn, new Point(0, moreBtn.Height));
    }

    private async Task RestartWebTab(WebTab tab)
    {
        if (tab.RestartBusy || IsDisposed) return;
        tab.RestartBusy = true;
        try
        {
            tab.State.ShowLoading(tab.Title);
            tab.NavDone = false;
            tab.RestartGen++;
            var gen = tab.RestartGen;
            var old = tab.View;
            if (old is not null)
            {
                try { tab.Page.Controls.Remove(old); } catch (Exception) { }
                try { old.Dispose(); } catch (Exception) { }
            }
            var host = TabHostClientSize();
            var view = new WebView2
            {
                Dock = DockStyle.None,
                Size = host,
                Bounds = new Rectangle(Point.Empty, host),
                DefaultBackgroundColor = HbcTheme.Bg,
            };
            tab.View = view;
            tab.Page.Controls.Add(view);
            var token = await SessionTokenAsync(force: true);
            if (token.Length > 0)
                buildUrl = HubBase + "/build.html?token=" + Uri.EscapeDataString(token);
            if (tab.IsBuild)
            {
                buildView = view;
                if (!string.IsNullOrEmpty(buildUrl)) tab.Url = buildUrl;
            }
            tab.State.BringToFront();
            var browserDied = tab.LastFail == CoreWebView2ProcessFailedKind.BrowserProcessExited;
            if (WebViewRestart.MustRecreateEnvironment(tab.Env is null, browserDied))
            {
                var folder = Path.Combine(root, "profiles", tab.Profile);
                await Task.Run(() => Directory.CreateDirectory(folder));
                tab.Env = await CoreWebView2Environment.CreateAsync(null, folder);
            }
            PrepareWebView(view);
            var ensure = view.EnsureCoreWebView2Async(tab.Env);
            var finished = await Task.WhenAny(ensure, Task.Delay(WebViewRestart.TimeoutMs));
            if (IsDisposed) return;
            if (finished != ensure)
            {
                tab.State.ShowError("Restart timed out.");
                Say(tab.Title + " restart timed out.");
                Program.Log("restart timeout " + tab.Title);
                return;
            }
            await ensure;
            await AttachOwnedWebView(view);
            tab.LastFail = null;
            HookWebTab(tab);
            view.Source = new Uri(tab.Url);
            _ = WatchRestartNavigation(tab, gen);
            Program.Log("restarted " + tab.Title);
        }
        catch (Exception ex)
        {
            Program.Log("restart " + tab.Title + " " + ex.Message);
            tab.State.ShowError(ex.Message);
            Say(tab.Title + " did not restart. " + ex.Message);
        }
        finally
        {
            tab.RestartBusy = false;
        }
    }

    private void OnWebViewProcessFailed(WebTab tab, WebView2 capturedView, CoreWebView2ProcessFailedKind kind)
    {
        if (capturedView.IsDisposed || !ReferenceEquals(tab.View, capturedView)) return;
        tab.LastFail = kind;
        if (IsDisposed || !IsHandleCreated) return;
        try
        {
            BeginInvoke(() =>
            {
                if (IsDisposed || !IsHandleCreated) return;
                if (capturedView.IsDisposed || !ReferenceEquals(tab.View, capturedView)) return;
                tab.State.ShowCrashed();
            });
        }
        catch (ObjectDisposedException) { }
        catch (InvalidOperationException) { }
    }

    private void HookBrowserProcessExited(WebTab tab)
    {
        var env = tab.Env;
        if (env is null || ReferenceEquals(tab.EnvExitHook, env)) return;
        tab.EnvExitHook = env;
        env.BrowserProcessExited += (_, _) =>
        {
            OnWebViewProcessFailed(tab, tab.View, CoreWebView2ProcessFailedKind.BrowserProcessExited);
        };
    }

    private void HookWebTab(WebTab tab)
    {
        var view = tab.View;
        tabViews[tab.Title] = view;
        AttachUsageHeaderWatch(view, tab.Title);
        view.GotFocus += (_, _) => DismissPopups();
        HookBrowserProcessExited(tab);
        var capturedCore = view.CoreWebView2;
        if (capturedCore is null) return;
        capturedCore.NewWindowRequested += async (_, e) =>
        {
            if (tab.Env is null) return;
            var deferral = e.GetDeferral();
            try { await OpenChild(tab.Title, tab.Env, e); }
            finally { deferral.Complete(); }
        };
        capturedCore.ProcessFailed += (sender, e) =>
        {
            if (view.IsDisposed || !ReferenceEquals(tab.View, view)) return;
            if (sender is not null && !ReferenceEquals(sender, capturedCore) && !ReferenceEquals(sender, view)) return;
            OnWebViewProcessFailed(tab, view, e.ProcessFailedKind);
        };
        capturedCore.NavigationCompleted += (_, e) =>
        {
            tab.NavDone = true;
            if (e.IsSuccess)
                tab.LastFail = null;
            else if (tab.LastFail is not null)
            {
                tab.State.ShowCrashed();
                return;
            }
            if (tab.IsBuild && tab.Banner is not null)
            {
                tab.Banner.Text = e.IsSuccess ? "Build tab is connected." : "Build tab failed to load. The hub may be down.";
                if (e.IsSuccess) tab.State.ShowLoaded();
                else
                {
                    tab.State.ShowError("hub");
                    Say("Build tab failed to load.");
                }
                return;
            }
            var current = view.Source;
            if (current is null) return;
            Program.Log("nav " + tab.Title + " " + current.Host + current.AbsolutePath);
            if (e.IsSuccess) tab.State.ShowLoaded();
            else tab.State.ShowError(e.WebErrorStatus.ToString());
            if (tab.Profile != "x" && current.Host.EndsWith("x.com", StringComparison.OrdinalIgnoreCase)
                && current.AbsolutePath.StartsWith("/home", StringComparison.OrdinalIgnoreCase))
            {
                ShowRetry(tab.Page, view, tab.Url);
            }
        };
    }

    private sealed class WebTab
    {
        public string Title = "";
        public string Url = "";
        public string Profile = "";
        public Panel Page = null!;
        public WebView2 View = null!;
        public TabStatePanel State = null!;
        public CoreWebView2Environment? Env;
        public CoreWebView2Environment? EnvExitHook;
        public CoreWebView2ProcessFailedKind? LastFail;
        public bool RestartBusy;
        public bool IsBuild;
        public bool NavDone;
        public int RestartGen;
        public Label? Banner;
    }

    private async Task WatchRestartNavigation(WebTab tab, int gen)
    {
        await Task.Delay(WebViewRestart.TimeoutMs);
        if (IsDisposed || tab.RestartGen != gen || tab.NavDone) return;
        tab.State.ShowError("Restart timed out.");
        Say(tab.Title + " restart timed out.");
        Program.Log("restart timeout " + tab.Title);
    }

    internal bool TrySelectTabShortcut(Keys keyData)
    {
        if (keyData == (Keys.Control | Keys.Tab))
        {
            if (pages.Count == 0) return true;
            SelectTab((activeTab + 1) % pages.Count);
            return true;
        }
        if (keyData == (Keys.Control | Keys.Shift | Keys.Tab))
        {
            if (pages.Count == 0) return true;
            SelectTab((activeTab - 1 + pages.Count) % pages.Count);
            return true;
        }
        var map = new[] { Keys.D1, Keys.D2, Keys.D3, Keys.D4, Keys.D5 };
        for (var i = 0; i < map.Length && i < TabOrder.Length; i++)
        {
            if (keyData != (Keys.Control | map[i])) continue;
            var index = IndexOf(TabOrder[i]);
            if (index >= 0) SelectTab(index);
            return true;
        }
        return false;
    }
}
