namespace Shell;

/// <summary>
/// Overlay so a web tab is never blank: Loading, Couldn't load, or Stopped working.
/// </summary>
internal sealed class TabStatePanel : Panel
{
    public const string LoadingText = "Loading";
    public const string ErrorText = "This page didn't load";
    public const string CrashedText = "This tab stopped working";

    private readonly Label title = new();
    private readonly Label detail = new();
    private readonly Button retry = new();
    private readonly Button openBrowser = new();
    private readonly Button showDetails = new();
    private string lastDetail = "";
    private bool allowOpenBrowser = true;

    public event EventHandler? Retry;
    public event EventHandler? OpenBrowser;

    public TabStatePanel()
    {
        Dock = DockStyle.Fill;
        BackColor = HbcTheme.Bg;
        ForeColor = HbcTheme.Text;
        Visible = true;
        title.AutoSize = true;
        title.Font = HbcTheme.Ui(12f, FontStyle.Bold);
        title.ForeColor = HbcTheme.Text;
        title.Location = new Point(24, 48);
        detail.AutoSize = true;
        detail.ForeColor = HbcTheme.Text2;
        detail.Location = new Point(24, 80);
        retry.Text = "Try again";
        retry.FlatStyle = FlatStyle.Flat;
        retry.BackColor = HbcTheme.Emerald;
        retry.ForeColor = HbcTheme.OnAccent;
        retry.Location = new Point(24, 120);
        retry.AutoSize = true;
        retry.Click += (_, _) => Retry?.Invoke(this, EventArgs.Empty);
        openBrowser.Text = "Open in browser";
        openBrowser.FlatStyle = FlatStyle.Flat;
        openBrowser.BackColor = HbcTheme.Surface2;
        openBrowser.ForeColor = HbcTheme.Text;
        openBrowser.Location = new Point(140, 120);
        openBrowser.AutoSize = true;
        openBrowser.Click += (_, _) => OpenBrowser?.Invoke(this, EventArgs.Empty);
        showDetails.Text = "Show details";
        showDetails.FlatStyle = FlatStyle.Flat;
        showDetails.BackColor = HbcTheme.Surface2;
        showDetails.ForeColor = HbcTheme.Text2;
        showDetails.Location = new Point(24, 164);
        showDetails.AutoSize = true;
        showDetails.Click += (_, _) =>
        {
            detail.Text = lastDetail.Length > 0 ? lastDetail : "No extra detail.";
            detail.Visible = true;
        };
        Controls.Add(title);
        Controls.Add(detail);
        Controls.Add(retry);
        Controls.Add(openBrowser);
        Controls.Add(showDetails);
        ShowLoading("this tab");
    }

    public void HideOpenBrowser()
    {
        allowOpenBrowser = false;
        openBrowser.Visible = false;
        if (detail.Text.IndexOf("Open in browser", StringComparison.OrdinalIgnoreCase) >= 0)
            detail.Text = "Taking a while? Wait or try again.";
    }

    public void ShowLoading(string tab)
    {
        title.Text = LoadingText + " " + tab + "…";
        detail.Text = allowOpenBrowser ? "Taking a while? Use Open in browser." : "Taking a while? Wait or try again.";
        lastDetail = "";
        retry.Visible = false;
        openBrowser.Visible = allowOpenBrowser;
        showDetails.Visible = false;
        Visible = true;
        BringToFront();
    }

    public void ShowError(string extra)
    {
        title.Text = ErrorText;
        detail.Text = "Check your internet, then try again.";
        lastDetail = extra ?? "";
        retry.Visible = true;
        openBrowser.Visible = allowOpenBrowser;
        showDetails.Visible = true;
        Visible = true;
        BringToFront();
    }

    public void ShowCrashed()
    {
        title.Text = CrashedText;
        detail.Text = "Other tabs are fine. Restarting this tab keeps you signed in.";
        lastDetail = "";
        retry.Text = "Restart this tab";
        retry.Visible = true;
        openBrowser.Visible = false;
        showDetails.Visible = false;
        Visible = true;
        BringToFront();
    }

    public void ShowLoaded()
    {
        Visible = false;
    }
}
