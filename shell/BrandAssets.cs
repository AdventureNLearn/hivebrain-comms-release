using System.Drawing.Drawing2D;

namespace Shell;

internal static class BrandAssets
{
    public const string ProductName = "LPIN";
    public const string PrivateDir = "local-brand";
    public const string FallbackSvg = "brand/lpin-compass-outline.svg";
    public const string PrivateIco = "compass-mark-private.ico";
    public const string PrivateHeader = "control-header-texture-private.jpg";

    public static readonly int[] MarkSizes = { 16, 32, 64, 256, 512 };

    public static string MarkPngName(int size) => "compass-mark-private-" + size + ".png";

    public static string FallbackSvgPath(string root) => Path.Combine(root, FallbackSvg);

    public static string? ExistingFile(string root, string name)
    {
        var path = Path.Combine(root, PrivateDir, name);
        if (!File.Exists(path)) return null;
        try
        {
            if (new FileInfo(path).Attributes.HasFlag(FileAttributes.ReparsePoint)) return null;
        }
        catch (IOException)
        {
            return null;
        }
        return path;
    }

    public static string Resolve(string root, string name) => ExistingFile(root, name) ?? FallbackSvgPath(root);

    public static string WindowIcon(string root) => Resolve(root, PrivateIco);

    public static string MarkPng(string root, int size) => Resolve(root, MarkPngName(size));

    public static string HeaderTexture(string root) => Resolve(root, PrivateHeader);

    public static bool IsFallback(string path, string root) =>
        string.Equals(Path.GetFullPath(path), Path.GetFullPath(FallbackSvgPath(root)), StringComparison.OrdinalIgnoreCase);

    public static void ApplyWindowIcon(Form form, string root)
    {
        var path = ExistingFile(root, PrivateIco);
        if (path is null) return;
        try { form.Icon = new Icon(path); }
        catch (Exception ex) { Program.Log("brand icon " + ex.Message); }
    }

    public static Image? LoadBitmap(string root, string name)
    {
        var path = ExistingFile(root, name);
        if (path is null) return null;
        try { return Image.FromFile(path); }
        catch (Exception ex)
        {
            Program.Log("brand image " + ex.Message);
            return null;
        }
    }

    public static Image OutlineBitmap(int size)
    {
        var bmp = new Bitmap(Math.Max(size, 16), Math.Max(size, 16));
        using var g = Graphics.FromImage(bmp);
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.Clear(Color.Transparent);
        var scale = bmp.Width / 64f;
        using var pen = new Pen(Color.FromArgb(0xf2, 0xf4, 0xf8), Math.Max(1.2f, 1.75f * scale))
        {
            StartCap = LineCap.Round,
            EndCap = LineCap.Round,
            LineJoin = LineJoin.Round,
        };
        g.DrawEllipse(pen, 12 * scale, 12 * scale, 40 * scale, 40 * scale);
        g.DrawEllipse(pen, 29 * scale, 29 * scale, 6 * scale, 6 * scale);
        g.DrawLine(pen, 32 * scale, 6 * scale, 32 * scale, 28 * scale);
        g.DrawLine(pen, 32 * scale, 36 * scale, 32 * scale, 58 * scale);
        g.DrawLine(pen, 6 * scale, 32 * scale, 28 * scale, 32 * scale);
        g.DrawLine(pen, 36 * scale, 32 * scale, 58 * scale, 32 * scale);
        g.DrawLine(pen, 18.6f * scale, 18.6f * scale, 26.5f * scale, 26.5f * scale);
        g.DrawLine(pen, 45.4f * scale, 45.4f * scale, 37.5f * scale, 37.5f * scale);
        g.DrawLine(pen, 45.4f * scale, 18.6f * scale, 37.5f * scale, 26.5f * scale);
        g.DrawLine(pen, 18.6f * scale, 45.4f * scale, 26.5f * scale, 37.5f * scale);
        return bmp;
    }

    public static Image MarkImage(string root, int size)
    {
        return LoadBitmap(root, MarkPngName(size))
            ?? LoadBitmap(root, MarkPngName(size <= 32 ? 32 : 64))
            ?? OutlineBitmap(size);
    }

    public static Control TitleBarMark(string root)
    {
        var image = MarkImage(root, 32);
        return new PictureBox
        {
            Image = image,
            SizeMode = PictureBoxSizeMode.Zoom,
            Size = new Size(28, 28),
            Location = new Point(10, 8),
            BackColor = Color.Transparent,
        };
    }

    public static Control TabIcon(string root)
    {
        var image = MarkImage(root, 32);
        return new PictureBox
        {
            Image = image,
            SizeMode = PictureBoxSizeMode.Zoom,
            Size = new Size(24, 24),
            Margin = new Padding(4, 6, 8, 0),
            BackColor = Color.Transparent,
        };
    }

    public static Image SplashMark(string root) =>
        LoadBitmap(root, MarkPngName(256)) ?? LoadBitmap(root, MarkPngName(512)) ?? OutlineBitmap(256);

    public static Image? ControlHeaderTexture(string root) => LoadBitmap(root, PrivateHeader);

    public static Form MakeSplash(string root)
    {
        var image = SplashMark(root);
        var form = new Form
        {
            FormBorderStyle = FormBorderStyle.None,
            StartPosition = FormStartPosition.CenterScreen,
            Size = new Size(320, 320),
            ShowInTaskbar = false,
            TopMost = true,
            BackColor = HbcTheme.Bg,
            Text = ProductName,
        };
        form.Controls.Add(new PictureBox
        {
            Image = image,
            Dock = DockStyle.Fill,
            SizeMode = PictureBoxSizeMode.Zoom,
            BackColor = Color.Transparent,
        });
        return form;
    }
}
