using System.Text.Json;
using Microsoft.Web.WebView2.WinForms;

namespace Shell;

public partial class Form1
{
    private static readonly Color Red = Color.FromArgb(0xc4, 0x56, 0x4a);
    private Panel controlPage = null!;
    private Label busBox = null!;
    private Label healthHubBox = null!;
    private Label healthBuildBox = null!;
    private Label signingBox = null!;
    private ListView usageView = null!;
    private ListView jobView = null!;
    private ListView needsView = null!;
    private TextBox feedBox = null!;
    private Button restartButton = null!;
    private readonly ToolTip restartTip = new();
    private DateTimeOffset? healthOkAt;
    private DateTimeOffset? usageOkAt;
    private DateTimeOffset? jobsOkAt;
    private DateTimeOffset? feedOkAt;
    private FileSystemWatcher? feedWatch;
    private System.Windows.Forms.Timer? healthTimer;
    private System.Windows.Forms.Timer? jobTimer;
    private System.Windows.Forms.Timer? feedDebounce;
    private bool healthTickBusy;
    private bool jobTickBusy;
    private bool feedTickBusy;
    internal const int FeedDebounceMs = 500;
    private readonly Dictionary<string, ControlLogic.JobRow> jobRows = new(StringComparer.Ordinal);
    private readonly List<ControlLogic.NeedRow> readyRows = new();
    private string handoffDir = "";
    private bool lastBuildPane;
    private bool lastRestartRoute = true;
    private bool lastStaging;
    private bool restartBusy;
    private DateTimeOffset lastRestartAt = DateTimeOffset.MinValue;
    private static readonly TimeSpan RestartMinInterval = TimeSpan.FromSeconds(3);
    private WebView2? buildView;
    private string? buildUrl;
    private readonly Dictionary<string, UsageRefreshLogic.DisplayRow> usageSnapshot = new(StringComparer.Ordinal);
    private bool usageBusy;
    private DateTimeOffset lastUsageCollect = DateTimeOffset.MinValue;
    private TextBox usageIntervalBox = null!;
    private System.Windows.Forms.Timer? usageTimer;

    private void AddControl()
    {
        MakeTab("Control");
        ControlLogic.BindLanes(root);
        handoffDir = ControlLogic.HandoffFolder(root);
        controlPage = new Panel { Dock = DockStyle.Fill, BackColor = Paper, Visible = false, AutoScroll = true };
        var stack = new TableLayoutPanel
        {
            Dock = DockStyle.Top,
            AutoSize = true,
            ColumnCount = 1,
            BackColor = Paper,
            Padding = new Padding(8),
        };
        stack.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));

        var header = new Panel { Height = 56, Dock = DockStyle.Top, BackColor = Panel };
        var texture = BrandAssets.ControlHeaderTexture(root);
        if (texture is not null)
        {
            header.BackgroundImage = texture;
            header.BackgroundImageLayout = ImageLayout.Zoom;
        }
        header.Controls.Add(new Label
        {
            Text = BrandAssets.ProductName,
            AutoSize = true,
            ForeColor = Ink,
            Location = new Point(8, 16),
            Font = new Font(Font, FontStyle.Bold),
        });
        stack.Controls.Add(header);

        busBox = SectionLabel();
        busBox.Text = "bus " + ControlLogic.HandoffLabel(root);
        healthHubBox = SectionLabel();
        healthBuildBox = SectionLabel();
        signingBox = SectionLabel();
        usageView = Grid("Feature", "Used", "Limit", "Reset", "Source", "Read");
        var limits = BuildUsageLimitEditor();
        jobView = Grid("Id", "Sender", "Routed", "Status");
        needsView = Grid("Id", "Reason");
        feedBox = new TextBox
        {
            Multiline = true,
            ReadOnly = true,
            ScrollBars = ScrollBars.Vertical,
            BackColor = Paper,
            ForeColor = Ink,
            Font = new Font("Consolas", 10),
            Height = 140,
            Dock = DockStyle.Top,
            WordWrap = false,
        };
        jobView.DoubleClick += (_, _) => OpenJobReturn();
        jobView.Click += (_, _) => { if (jobView.SelectedItems.Count > 0) OpenJobReturn(); };
        usageView.Click += (_, _) => OpenUsageLink();

        var buttons = new FlowLayoutPanel { AutoSize = true, BackColor = Paper, Dock = DockStyle.Top };
        restartButton = new Button { Text = "Restart Build only", FlatStyle = FlatStyle.Flat, ForeColor = Ink, BackColor = Panel, AutoSize = true };
        var start = new Button { Text = "Start All", FlatStyle = FlatStyle.Flat, ForeColor = Paper, BackColor = Green, AutoSize = true };
        var refreshUsage = new Button { Text = "Refresh usage", FlatStyle = FlatStyle.Flat, ForeColor = Ink, BackColor = Panel, AutoSize = true };
        usageIntervalBox = new TextBox
        {
            Width = 48,
            Text = usageRefreshMinutes.ToString(System.Globalization.CultureInfo.InvariantCulture),
            BackColor = HbcTheme.Surface2,
            ForeColor = Ink,
            BorderStyle = BorderStyle.FixedSingle,
        };
        var intervalLabel = new Label { Text = "Interval minutes (0=off, min 5)", AutoSize = true, ForeColor = Ink, Padding = new Padding(8, 6, 0, 0) };
        restartTip.SetToolTip(restartButton, "Asks the hub to restart its Build pane. Never launches grok from this app.");
        restartButton.Click += async (_, _) => await RunRestartBuild();
        start.Click += async (_, _) => await RunStartAll();
        refreshUsage.Click += async (_, _) => await RefreshUsageNow();
        usageIntervalBox.Leave += (_, _) => ApplyUsageIntervalFromBox();
        buttons.Controls.Add(restartButton);
        buttons.Controls.Add(start);
        buttons.Controls.Add(refreshUsage);
        buttons.Controls.Add(usageIntervalBox);
        buttons.Controls.Add(intervalLabel);

        stack.Controls.Add(Head("Health"));
        stack.Controls.Add(busBox);
        stack.Controls.Add(healthHubBox);
        stack.Controls.Add(healthBuildBox);
        stack.Controls.Add(Head("Signing"));
        stack.Controls.Add(signingBox);
        stack.Controls.Add(Head("Usage"));
        stack.Controls.Add(Wrap(usageView, 140));
        stack.Controls.Add(Head("Your limits (typed here; nothing is pre-filled)"));
        stack.Controls.Add(limits);
        stack.Controls.Add(Head("Job board"));
        stack.Controls.Add(Wrap(jobView, 180));
        stack.Controls.Add(Head("Needs-you"));
        stack.Controls.Add(Wrap(needsView, 100));
        stack.Controls.Add(Head("Status feed"));
        stack.Controls.Add(feedBox);
        stack.Controls.Add(buttons);
        controlPage.Controls.Add(stack);
        pages.Add(controlPage);
        Controls.Add(controlPage);
        controlPage.BringToFront();

        var controlReady = new Label
        {
            Text = "Control ready",
            AutoSize = true,
            ForeColor = Green,
            Font = HbcTheme.Ui(10f, FontStyle.Bold),
            Margin = new Padding(0, 0, 0, 8),
        };
        stack.Controls.Add(controlReady);
        stack.Controls.SetChildIndex(controlReady, 0);
        WatchStatusFeed();
        healthTimer = new System.Windows.Forms.Timer { Interval = 5000 };
        healthTimer.Tick += OnHealthTick;
        healthTimer.Start();
        jobTimer = new System.Windows.Forms.Timer { Interval = 10000 };
        jobTimer.Tick += OnJobTick;
        jobTimer.Start();
        ApplyUsageIntervalTimer();
        _ = RefreshHealth();
        _ = RefreshUsage();
        _ = RefreshJobs();
        _ = RefreshFeed();
    }

    private async void OnHealthTick(object? sender, EventArgs e)
    {
        if (healthTickBusy) return;
        healthTickBusy = true;
        try
        {
            await RefreshHealth();
            await RefreshFeed();
        }
        finally { healthTickBusy = false; }
    }

    private async void OnJobTick(object? sender, EventArgs e)
    {
        if (jobTickBusy) return;
        jobTickBusy = true;
        try
        {
            await RefreshUsage();
            await RefreshJobs();
        }
        finally { jobTickBusy = false; }
    }

    private static Label Head(string title) => new()
    {
        Text = title,
        AutoSize = true,
        ForeColor = Color.FromArgb(0xf2, 0xf4, 0xf8),
        Font = new Font("Segoe UI", 11, FontStyle.Bold),
        Margin = new Padding(0, 8, 0, 2),
    };

    private Label SectionLabel() => new()
    {
        AutoSize = true,
        ForeColor = Ink,
        BackColor = Paper,
        Font = new Font("Consolas", 10),
        MaximumSize = new Size(1100, 0),
    };

    private ListView Grid(params string[] columns)
    {
        var view = new ListView
        {
            View = View.Details,
            FullRowSelect = true,
            HeaderStyle = ColumnHeaderStyle.Nonclickable,
            BackColor = Paper,
            ForeColor = Ink,
            Dock = DockStyle.Fill,
            Font = new Font("Segoe UI", 10),
        };
        foreach (var column in columns)
            view.Columns.Add(column, 140);
        return view;
    }

    private static Panel Wrap(Control child, int height)
    {
        var box = new Panel { Height = height, Dock = DockStyle.Top };
        child.Dock = DockStyle.Fill;
        box.Controls.Add(child);
        return box;
    }

    private void WatchStatusFeed()
    {
        try
        {
            if (!Directory.Exists(handoffDir)) return;
            feedWatch = new FileSystemWatcher(handoffDir, ControlLogic.StatusFile)
            {
                NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite | NotifyFilters.Size,
            };
            feedWatch.Changed += (_, _) => PostFeedRefresh();
            feedWatch.Created += (_, _) => PostFeedRefresh();
            feedWatch.EnableRaisingEvents = true;
        }
        catch (Exception ex)
        {
            Program.Log("feed watch " + ex.Message);
        }
    }

    private void PostFeedRefresh()
    {
        if (IsDisposed || !IsHandleCreated) return;
        try { BeginInvoke(ScheduleFeedRefresh); }
        catch (ObjectDisposedException) { }
        catch (InvalidOperationException) { }
    }

    private void ScheduleFeedRefresh()
    {
        if (IsDisposed) return;
        if (feedDebounce is null)
        {
            feedDebounce = new System.Windows.Forms.Timer { Interval = FeedDebounceMs };
            feedDebounce.Tick += OnFeedDebounceTick;
        }
        feedDebounce.Stop();
        feedDebounce.Start();
    }

    private async void OnFeedDebounceTick(object? sender, EventArgs e)
    {
        feedDebounce?.Stop();
        await RefreshFeed();
    }

    private void ApplyBuildHealth(bool buildUp)
    {
        healthBuildBox.Text = buildUp ? "hub pane alive" : "hub pane down";
        healthBuildBox.ForeColor = buildUp ? Green : Red;
    }

    private async Task RefreshHealth()
    {
        if (IsDisposed) return;
        ApplyBuildHealth(lastBuildPane);
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
            var response = await client.GetAsync(HubBase + "/status");
            if (!response.IsSuccessStatusCode) throw new InvalidOperationException("hub down");
            using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
            healthOkAt = DateTimeOffset.Now;
            lastBuildPane = doc.RootElement.TryGetProperty("build_pane", out var paneEl) && paneEl.GetBoolean();
            lastRestartRoute = !doc.RootElement.TryGetProperty("restart_build", out var routeEl) || routeEl.GetBoolean();
            lastStaging = doc.RootElement.TryGetProperty("staging", out var stagingEl) && stagingEl.ValueKind == JsonValueKind.True;
            if (doc.RootElement.TryGetProperty("handoff", out var handoffEl))
            {
                var label = handoffEl.GetString() ?? "";
                if (label.Length > 0) busBox.Text = "bus " + label;
            }
            var signing = doc.RootElement.TryGetProperty("signing", out var mode) ? mode.GetString() ?? "v2" : "v2";
            var v1 = doc.RootElement.TryGetProperty("v1_found", out var v1El) && v1El.GetBoolean();
            signingBox.Text = v1 || signing == "v1" ? "v1" : "v2";
            signingBox.ForeColor = signingBox.Text == "v1" ? Red : Green;
            readyRows.Clear();
            if (doc.RootElement.TryGetProperty("ready", out var readyEl) && readyEl.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in readyEl.EnumerateArray())
                {
                    if (item.ValueKind != JsonValueKind.Object) continue;
                    var id = item.TryGetProperty("id", out var idEl) ? idEl.GetString() ?? "" : "";
                    if (id.Length == 0) continue;
                    readyRows.Add(new ControlLogic.NeedRow
                    {
                        Id = id,
                        Reason = item.TryGetProperty("reason", out var reasonEl) ? reasonEl.GetString() ?? "ready" : "ready",
                    });
                }
            }
            var events = await Task.Run(() => ControlLogic.ReadEvents(root, 10));
            void applyEvents()
            {
                if (IsDisposed) return;
                var lines = new List<string> { "hub on" };
                foreach (var row in events)
                {
                    var when = ControlLogic.FriendlyEt(row.Time);
                    var extra = "";
                    if (!string.IsNullOrEmpty(row.Pane)) extra += " " + row.Pane;
                    if (!string.IsNullOrEmpty(row.Exit)) extra += " exit " + row.Exit;
                    lines.Add((when.Length > 0 ? when : row.Time) + "  " + row.Event + extra);
                }
                if (events.Count == 0)
                    lines.Add("event log empty (PR #6 writes " + ControlLogic.EventLogName + ")");
                healthHubBox.Text = string.Join(Environment.NewLine, lines);
                healthHubBox.ForeColor = Green;
                RefreshNeeds();
            }
            if (InvokeRequired) BeginInvoke(applyEvents);
            else applyEvents();
        }
        catch (Exception)
        {
            lastBuildPane = false;
            lastRestartRoute = false;
            var age = healthOkAt is null ? "never" : ControlLogic.AgeLabel(healthOkAt.Value, DateTimeOffset.Now);
            healthHubBox.Text = "UNKNOWN   last read " + age;
            healthHubBox.ForeColor = Red;
            signingBox.Text = "UNKNOWN";
            signingBox.ForeColor = Red;
        }
        ApplyBuildHealth(lastBuildPane);
        ApplyRestartButton();
    }

    private void ApplyRestartButton()
    {
        var allowed = ControlLogic.BuildClaimReady && lastRestartRoute && !restartBusy && !lastStaging;
        restartButton.Enabled = allowed;
        restartTip.SetToolTip(
            restartButton,
            ControlLogic.BuildClaimReady
                ? (lastStaging
                    ? "Staging hub: Build watcher is off."
                    : lastRestartRoute
                    ? "Asks the hub to restart its Build pane. Never launches grok from this app."
                    : "Restart Build is disabled: the hub has no restart-build route or the hub is down.")
                : ControlLogic.HeldUntilBuildClaim);
    }

    private async Task RefreshUsage()
    {
        if (IsDisposed) return;
        try
        {
            var rows = await Task.Run(() => ControlLogic.UsageRows());
            void applyUsage()
            {
                if (IsDisposed) return;
                usageOkAt = DateTimeOffset.Now;
                usageView.BeginUpdate();
                usageView.Items.Clear();
                foreach (var row in rows)
                {
                    if (usageSnapshot.TryGetValue(row.Id, out var snap))
                    {
                        row.Used = snap.Used;
                        row.Limit = snap.Limit;
                        row.Reset = snap.Reset;
                        row.Source = snap.Source;
                        row.ReadAt = snap.ReadAt;
                    }
                    var item = new ListViewItem(row.Label);
                    item.SubItems.Add(row.Used);
                    item.SubItems.Add(row.Limit);
                    item.SubItems.Add(row.Reset);
                    item.SubItems.Add(row.Source);
                    item.SubItems.Add(row.ReadAt);
                    item.Tag = row.Id;
                    usageView.Items.Add(item);
                }
                usageView.EndUpdate();
            }
            if (InvokeRequired) BeginInvoke(applyUsage);
            else applyUsage();
        }
        catch (Exception)
        {
            void applyUnknown()
            {
                if (IsDisposed) return;
                usageView.BeginUpdate();
                usageView.Items.Clear();
                usageView.Items.Add(new ListViewItem("UNKNOWN"));
                usageView.EndUpdate();
            }
            if (InvokeRequired) BeginInvoke(applyUnknown);
            else applyUnknown();
        }
    }

    private async Task RefreshUsageNow()
    {
        if (IsDisposed || usageBusy) return;
        usageBusy = true;
        try
        {
            var now = DateTimeOffset.Now;
            var manual = await Task.Run(UsageStore.Load);
            var collected = new Dictionary<string, UsageRefreshLogic.Reading>(StringComparer.Ordinal);
            collected["supergrok"] = await ReadWebFeature("SuperGrok", "supergrok");
            collected["imagine"] = await ReadWebFeature("Imagine", "imagine");
            collected["x-reads"] = await ReadWebFeature("X", "x-reads");
            collected["x-posts"] = await ReadXPosts();
            collected["build-cli"] = await ReadBuildState();
            usageSnapshot.Clear();
            foreach (var (id, label, _) in ControlLogic.Features)
            {
                collected.TryGetValue(id, out var reading);
                manual.TryGetValue(id, out var saved);
                usageSnapshot[id] = UsageRefreshLogic.Merge(id, label, reading, saved, now);
                Program.Log(UsageRefreshLogic.LogLine(id, reading?.Status ?? "absent"));
            }
            lastUsageCollect = now;
            await RefreshUsage();
        }
        catch (Exception ex)
        {
            Program.Log("usage parse error");
            _ = ex;
        }
        finally
        {
            usageBusy = false;
        }
    }

    private async Task<UsageRefreshLogic.Reading> ReadWebFeature(string title, string site)
    {
        var header = UsageRefreshLogic.FromHeaders(usageHeaders.TryGetValue(title, out var snap) ? snap : null);
        if (header.Status == "ok")
            return header;
        tabViews.TryGetValue(title, out var view);
        var payload = await ReadDomText(view, site);
        return site switch
        {
            "supergrok" => UsageRefreshLogic.ParseSuperGrokDom(payload),
            "imagine" => UsageRefreshLogic.ParseImagineDom(payload),
            "x-reads" => UsageRefreshLogic.ParseXReadsDom(payload),
            "x-posts" => UsageRefreshLogic.ParseXPostsDom(payload),
            _ => new UsageRefreshLogic.Reading { Status = "absent" },
        };
    }

    private async Task<UsageRefreshLogic.Reading> ReadXPosts()
    {
        var api = await ReadXApiUsage();
        if (api.Status == "ok")
            return api;
        return await ReadWebFeature("X", "x-posts");
    }

    private async Task<UsageRefreshLogic.Reading> ReadXApiUsage()
    {
        var token = await Task.Run(() => UsageSecrets.ReadXBearer(UsageStore.StoreDir));
        if (token.Length == 0)
            return new UsageRefreshLogic.Reading { Status = "absent", SourceText = "x-api off" };
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromMilliseconds(UsageRefreshLogic.TabTimeoutMs) };
            using var request = new HttpRequestMessage(HttpMethod.Get, UsageRefreshLogic.XApiUrl);
            request.Headers.TryAddWithoutValidation("Authorization", "Bearer " + token);
            using var response = await client.SendAsync(request);
            if (!response.IsSuccessStatusCode)
                return new UsageRefreshLogic.Reading { Status = "error", Source = UsageRefreshLogic.SourceApi };
            var body = await response.Content.ReadAsStringAsync();
            return UsageRefreshLogic.ParseXApi(body);
        }
        catch (Exception)
        {
            return new UsageRefreshLogic.Reading { Status = "error", Source = UsageRefreshLogic.SourceApi };
        }
    }

    private async Task<UsageRefreshLogic.Reading> ReadBuildState()
    {
        try
        {
            var token = await SessionTokenAsync();
            if (token.Length == 0) token = await SessionTokenAsync(force: true);
            if (token.Length == 0)
                return new UsageRefreshLogic.Reading { Status = "absent" };
            using var client = new HttpClient { Timeout = TimeSpan.FromMilliseconds(UsageRefreshLogic.TabTimeoutMs) };
            using var response = await SendHubState(client, token);
            if (TokenRejected(response.StatusCode))
            {
                token = await SessionTokenAsync(force: true);
                if (token.Length == 0)
                    return new UsageRefreshLogic.Reading { Status = "absent" };
                using var retried = await SendHubState(client, token);
                if (!retried.IsSuccessStatusCode)
                    return new UsageRefreshLogic.Reading { Status = "error", Source = UsageRefreshLogic.SourceApi };
                return UsageRefreshLogic.ParseBuildState(await retried.Content.ReadAsStringAsync());
            }
            if (!response.IsSuccessStatusCode)
                return new UsageRefreshLogic.Reading { Status = "error", Source = UsageRefreshLogic.SourceApi };
            var body = await response.Content.ReadAsStringAsync();
            return UsageRefreshLogic.ParseBuildState(body);
        }
        catch (Exception)
        {
            return new UsageRefreshLogic.Reading { Status = "error", Source = UsageRefreshLogic.SourceApi };
        }
    }

    private async Task<string> ReadDomText(WebView2? view, string site)
    {
        if (view?.CoreWebView2 is null) return "";
        if (!UsageRefreshLogic.ScriptIsReadOnly(UsageRefreshLogic.DomScript(site)))
            return "";
        try
        {
            var script = UsageRefreshLogic.DomScript(site);
            var task = view.ExecuteScriptAsync(script);
            var done = await Task.WhenAny(task, Task.Delay(UsageRefreshLogic.TabTimeoutMs));
            if (done != task) return "";
            return await task ?? "";
        }
        catch (Exception)
        {
            return "";
        }
    }

    private void AttachUsageHeaderWatch(WebView2 view, string title)
    {
        if (view.CoreWebView2 is null) return;
        view.CoreWebView2.WebResourceResponseReceived += (_, e) =>
        {
            try
            {
                if (!UsageRefreshLogic.RateLimitEndpointOk(e.Request?.Uri))
                    return;
                string? Get(string name)
                {
                    try { return e.Response?.Headers?.GetHeader(name); }
                    catch (Exception) { return null; }
                }
                var snap = UsageRefreshLogic.ReadRateLimitHeaders(Get);
                if (snap.HasAny)
                    usageHeaders[title] = snap;
            }
            catch (Exception)
            {
            }
        };
    }

    private void ApplyUsageIntervalFromBox()
    {
        if (!int.TryParse(usageIntervalBox.Text.Trim(), System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out var raw))
            raw = 0;
        usageRefreshMinutes = UsageRefreshLogic.ClampIntervalMinutes(raw);
        usageIntervalBox.Text = usageRefreshMinutes.ToString(System.Globalization.CultureInfo.InvariantCulture);
        SaveSettings();
        ApplyUsageIntervalTimer();
    }

    private void ApplyUsageIntervalTimer()
    {
        usageTimer?.Stop();
        if (usageRefreshMinutes < UsageRefreshLogic.MinIntervalMinutes)
            return;
        usageTimer ??= new System.Windows.Forms.Timer();
        usageTimer.Interval = usageRefreshMinutes * 60 * 1000;
        usageTimer.Tick -= UsageTimerTick;
        usageTimer.Tick += UsageTimerTick;
        usageTimer.Start();
    }

    private async void UsageTimerTick(object? sender, EventArgs e)
    {
        if (usageBusy) return;
        if (!UsageRefreshLogic.ShouldRunInterval(usageRefreshMinutes, WindowState == FormWindowState.Minimized, lastUsageCollect, DateTimeOffset.Now))
            return;
        await RefreshUsageNow();
    }

    private async Task RefreshJobs()
    {
        if (IsDisposed) return;
        var handoff = handoffDir;
        var approvals = ControlLogic.ApprovalsPath(root);
        try
        {
            var snap = await Task.Run(() => ControlLogic.LoadRefresh(handoff, approvals));
            void applyJobs()
            {
                if (IsDisposed) return;
                jobsOkAt = DateTimeOffset.Now;
                jobRows.Clear();
                jobView.BeginUpdate();
                jobView.Items.Clear();
                foreach (var row in snap.Jobs)
                {
                    jobRows[row.Id] = row;
                    var item = new ListViewItem(row.Id);
                    item.SubItems.Add(row.Sender);
                    item.SubItems.Add(row.RoutedEt);
                    item.SubItems.Add(row.Status);
                    item.Tag = row.Id;
                    jobView.Items.Add(item);
                }
                jobView.EndUpdate();
                readyRows.Clear();
                readyRows.AddRange(snap.Ready);
                RefreshNeeds();
            }
            if (InvokeRequired) BeginInvoke(applyJobs);
            else applyJobs();
        }
        catch (Exception)
        {
            void applyUnknown()
            {
                if (IsDisposed) return;
                jobView.BeginUpdate();
                jobView.Items.Clear();
                jobView.Items.Add(new ListViewItem("UNKNOWN"));
                jobView.EndUpdate();
            }
            if (InvokeRequired) BeginInvoke(applyUnknown);
            else applyUnknown();
        }
    }

    private void RefreshNeeds()
    {
        try
        {
            var rows = ControlLogic.NeedsYou(readyRows);
            needsView.BeginUpdate();
            needsView.Items.Clear();
            foreach (var row in rows)
            {
                var item = new ListViewItem(row.Id);
                item.SubItems.Add(row.Reason);
                needsView.Items.Add(item);
            }
            needsView.EndUpdate();
        }
        catch (Exception)
        {
            needsView.Items.Clear();
            needsView.Items.Add(new ListViewItem("UNKNOWN"));
        }
    }

    private async Task RefreshFeed()
    {
        if (IsDisposed || feedTickBusy) return;
        feedTickBusy = true;
        try
        {
            var handoff = handoffDir;
            var snap = await Task.Run(() => ControlLogic.LoadFeed(handoff));
            void applyFeed()
            {
                if (IsDisposed) return;
                feedOkAt = DateTimeOffset.Now;
                if (snap.Rows.Count == 0)
                {
                    feedBox.Text = snap.FileFound
                        ? ""
                        : ControlLogic.StatusFile + " was not found.";
                    return;
                }
                feedBox.Text = string.Join(Environment.NewLine, snap.Rows.Select(row =>
                    (row.TimeEt.Length > 0 ? row.TimeEt : row.Time) + "  " + row.Text));
            }
            if (InvokeRequired) BeginInvoke(applyFeed);
            else applyFeed();
        }
        catch (Exception)
        {
            void applyUnknown()
            {
                if (IsDisposed) return;
                feedBox.Text = "UNKNOWN";
            }
            if (InvokeRequired) BeginInvoke(applyUnknown);
            else applyUnknown();
        }
        finally { feedTickBusy = false; }
    }

    private void OpenJobReturn()
    {
        if (jobView.SelectedItems.Count == 0) return;
        var id = jobView.SelectedItems[0].Tag as string ?? jobView.SelectedItems[0].Text;
        var text = ControlLogic.JobReturnDisplay(handoffDir, id);
        if (text.Length == 0) text = "(no return)";
        var box = new Form
        {
            Text = id,
            Width = 720,
            Height = 480,
            StartPosition = FormStartPosition.CenterParent,
            BackColor = Paper,
            ForeColor = Ink,
        };
        var view = new TextBox
        {
            Multiline = true,
            ReadOnly = true,
            ScrollBars = ScrollBars.Both,
            WordWrap = false,
            Dock = DockStyle.Fill,
            BackColor = Paper,
            ForeColor = Ink,
            Font = new Font("Consolas", 10),
            Text = text,
        };
        box.Controls.Add(view);
        box.ShowDialog(this);
    }

    private void OpenUsageLink()
    {
        if (usageView.SelectedItems.Count == 0) return;
        var id = usageView.SelectedItems[0].Tag as string ?? "";
        var url = ControlLogic.FeatureLink(id);
        if (!ControlLogic.AllowedUsageLink(url)) return;
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            Program.Log("usage link " + ex.Message);
        }
    }

    private async Task RunRestartBuild()
    {
        if (restartBusy) return;
        if (lastStaging)
        {
            Say("Staging hub: Build watcher is off.");
            return;
        }
        if (DateTimeOffset.Now - lastRestartAt < RestartMinInterval) return;
        if (!ControlLogic.BuildClaimReady)
        {
            ApplyRestartButton();
            return;
        }
        restartBusy = true;
        ApplyRestartButton();
        try
        {
            var log = ControlLogic.ApprovalsPath(root);
            await ControlActions.RestartBuildOnly(new WinProcessManager(this), log, Ask);
            lastRestartAt = DateTimeOffset.Now;
            await RefreshHealth();
        }
        catch (Exception ex)
        {
            Say("Restart Build only failed. " + ex.Message);
        }
        finally
        {
            restartBusy = false;
            ApplyRestartButton();
        }
    }

    private async Task RunStartAll()
    {
        try
        {
            var log = ControlLogic.ApprovalsPath(root);
            await ControlActions.StartAll(new WinProcessManager(this), log, Ask);
            await RefreshHub();
        }
        catch (Exception ex)
        {
            Say("Start All failed. " + ex.Message);
        }
    }

    private bool Ask(string message) =>
        MessageBox.Show(this, message, BrandAssets.ProductName, MessageBoxButtons.YesNo) == DialogResult.Yes;

    internal bool VisibleGrokBuildTerminal()
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
                    return true;
                }
                catch (Exception)
                {
                    // skip
                }
            }
        }
        return false;
    }

    internal bool RestartRouteAvailable() => lastRestartRoute;

    internal bool BuildWatcherRunning(bool buildPane) => buildPane || VisibleGrokBuildTerminal();

    internal void FocusBuildTab()
    {
        var index = IndexOf("Build");
        if (index >= 0) SelectTab(index);
        if (lastStaging) return;
        if (buildView is not null && buildUrl is not null)
        {
            try { buildView.Source = new Uri(buildUrl); }
            catch (Exception ex) { Program.Log("build tab " + ex.Message); }
        }
    }

    private Panel BuildUsageLimitEditor()
    {
        var box = new Panel { AutoSize = true, Dock = DockStyle.Top, BackColor = Paper };
        var saved = UsageStore.Load();
        var rows = new List<(string Id, TextBox Used, TextBox Limit)>();
        var top = 0;
        foreach (var (id, label, _) in ControlLogic.Features)
        {
            saved.TryGetValue(id, out var row);
            var name = new Label { Text = label, AutoSize = true, ForeColor = Ink, Location = new Point(0, top + 4) };
            var used = new TextBox
            {
                Width = 80,
                Location = new Point(260, top),
                BackColor = HbcTheme.Surface2,
                ForeColor = Ink,
                BorderStyle = BorderStyle.FixedSingle,
                Text = UsageStore.DisplayOrBlank(row?.Used),
                PlaceholderText = "used",
            };
            var limit = new TextBox
            {
                Width = 80,
                Location = new Point(350, top),
                BackColor = HbcTheme.Surface2,
                ForeColor = Ink,
                BorderStyle = BorderStyle.FixedSingle,
                Text = UsageStore.DisplayOrBlank(row?.Limit),
                PlaceholderText = "limit",
            };
            box.Controls.Add(name);
            box.Controls.Add(used);
            box.Controls.Add(limit);
            rows.Add((id, used, limit));
            top += 32;
        }
        var save = new Button
        {
            Text = "Save limits",
            FlatStyle = FlatStyle.Flat,
            ForeColor = HbcTheme.OnAccent,
            BackColor = Green,
            Location = new Point(0, top + 4),
            AutoSize = true,
        };
        save.Click += (_, _) =>
        {
            UsageStore.Save(rows.Select(item => new UsageStore.Row
            {
                Feature = item.Id,
                Used = item.Used.Text.Trim(),
                Limit = item.Limit.Text.Trim(),
                Source = "manual",
            }));
            Say("Saved usage limits on this PC.");
        };
        box.Controls.Add(save);
        box.Height = top + 40;
        return box;
    }

    internal async Task<bool> RestartBuildPaneAsync()
    {
        var token = await SessionTokenAsync();
        if (token.Length == 0) token = await SessionTokenAsync(force: true);
        if (token.Length == 0) return false;
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
        var url = HubBase + "/api/restart-build?token=" + Uri.EscapeDataString(token);
        var response = await client.PostAsync(url, new StringContent(""));
        if (TokenRejected(response.StatusCode))
        {
            token = await SessionTokenAsync(force: true);
            if (token.Length == 0) return false;
            url = HubBase + "/api/restart-build?token=" + Uri.EscapeDataString(token);
            response = await client.PostAsync(url, new StringContent(""));
        }
        if (!response.IsSuccessStatusCode) return false;
        FocusBuildTab();
        return true;
    }
}

internal sealed class WinProcessManager : IProcessManager
{
    private readonly Form1 host;

    public WinProcessManager(Form1 host) => this.host = host;

    public Task<bool> HubRunning() => Form1.HubUp();

    public Task StartHub() => host.StartHubPublic();

    public Task FocusBuildTab()
    {
        host.FocusBuildTab();
        return Task.CompletedTask;
    }

    public bool HasRestartRoute() => host.RestartRouteAvailable();

    public Task<bool> RestartBuildPane() => host.RestartBuildPaneAsync();

    public ControlLogic.JobRow? RunningJob() =>
        ControlLogic.RunningJob(ControlLogic.HandoffFolder(host.RootPath), ControlLogic.ApprovalsPath(host.RootPath));

    public async Task<bool> WaitUntilBuildIdle()
    {
        var deadline = DateTime.UtcNow.AddMinutes(5);
        while (DateTime.UtcNow < deadline)
        {
            if (RunningJob() is null) return true;
            await Task.Delay(1000);
        }
        return false;
    }
}
