// HbcTheme.cs - WinForms side of the HiveBrainComms design tokens (v1, 2026-10-11).
// Keep in sync with tokens/hbc-tokens.css. No secrets or personal data belong in this file.
using System.Drawing;

namespace HiveBrainComms.UI
{
    public static class HbcTheme
    {
        // Surfaces
        public static readonly Color Bg           = ColorTranslator.FromHtml("#060d1a");
        public static readonly Color Surface      = ColorTranslator.FromHtml("#0a1628");
        public static readonly Color Surface2     = ColorTranslator.FromHtml("#0f2036");
        public static readonly Color Surface3     = ColorTranslator.FromHtml("#16293f");
        public static readonly Color Border       = ColorTranslator.FromHtml("#1f3a57");
        public static readonly Color BorderStrong = ColorTranslator.FromHtml("#2d5277");
        // Text
        public static readonly Color Text   = ColorTranslator.FromHtml("#e6f1ff");
        public static readonly Color Text2  = ColorTranslator.FromHtml("#9fb3c8");
        public static readonly Color Text3  = ColorTranslator.FromHtml("#8aa0b8");
        // Accent
        public static readonly Color Emerald  = ColorTranslator.FromHtml("#00e59a");
        public static readonly Color Gold     = ColorTranslator.FromHtml("#d4af37");
        public static readonly Color Cyan     = ColorTranslator.FromHtml("#00d4ff");
        public static readonly Color OnAccent = ColorTranslator.FromHtml("#06231a");
        // Status
        public static readonly Color Ok    = Emerald;
        public static readonly Color Warn  = ColorTranslator.FromHtml("#ffb84d");
        public static readonly Color Error = ColorTranslator.FromHtml("#ff5c5c");
        public static readonly Color Info  = Cyan;
        public static readonly Color Off   = ColorTranslator.FromHtml("#6f8299");

        // Type (Segoe UI Variable ships with Windows 11; falls back to Segoe UI)
        public static Font Ui(float size = 10f, FontStyle style = FontStyle.Regular)
            => new Font(FontFamilyOr("Segoe UI Variable Text", "Segoe UI"), size, style, GraphicsUnit.Point);
        public static Font Mono(float size = 10f)
            => new Font(FontFamilyOr("Cascadia Mono", "Consolas"), size, FontStyle.Regular, GraphicsUnit.Point);

        // Layout (logical px at 100% DPI; scale with DeviceDpi / 96f)
        public const int TitleBarHeight = 44, TabBarHeight = 44, ToolbarHeight = 36, StatusBarHeight = 26;
        public const int S1 = 4, S2 = 8, S3 = 12, S4 = 16, S5 = 24, S6 = 32;
        public const int RadiusSm = 6, RadiusMd = 10, RadiusLg = 16;

        private static string FontFamilyOr(string preferred, string fallback)
        {
            foreach (var f in FontFamily.Families) if (f.Name == preferred) return preferred;
            return fallback;
        }
    }
}
