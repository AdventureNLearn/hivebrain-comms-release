using System.Text.Json;

namespace Shell;

/// <summary>
/// User-typed usage counts and limits. Nothing is shipped with real-looking numbers.
/// Stored under LocalAppData, never in the repo.
/// </summary>
internal static class UsageStore
{
    public const string FileName = "usage.json";

    public static string StoreDir =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "HiveBrainComms");

    public static string StorePath => Path.Combine(StoreDir, FileName);

    public sealed class Row
    {
        public string Feature { get; set; } = "";
        public string Used { get; set; } = "";
        public string Limit { get; set; } = "";
        public string ResetRule { get; set; } = "never";
        public string Source { get; set; } = "manual";
    }

    public static Dictionary<string, Row> Load()
    {
        var found = new Dictionary<string, Row>(StringComparer.Ordinal);
        try
        {
            if (!File.Exists(StorePath)) return found;
            using var doc = JsonDocument.Parse(File.ReadAllText(StorePath));
            if (doc.RootElement.ValueKind != JsonValueKind.Array) return found;
            foreach (var item in doc.RootElement.EnumerateArray())
            {
                if (item.ValueKind != JsonValueKind.Object) continue;
                var feature = item.TryGetProperty("feature", out var idEl) ? idEl.GetString() ?? "" : "";
                if (feature.Length == 0) continue;
                found[feature] = new Row
                {
                    Feature = feature,
                    Used = item.TryGetProperty("used", out var usedEl) ? usedEl.GetString() ?? "" : "",
                    Limit = item.TryGetProperty("limit", out var limitEl) ? limitEl.GetString() ?? "" : "",
                    ResetRule = item.TryGetProperty("resetRule", out var resetEl) ? resetEl.GetString() ?? "never" : "never",
                    Source = "manual",
                };
            }
        }
        catch (Exception)
        {
        }
        return found;
    }

    public static void Save(IEnumerable<Row> rows)
    {
        Directory.CreateDirectory(StoreDir);
        var payload = rows.Select(row => new Dictionary<string, string>
        {
            ["feature"] = row.Feature,
            ["used"] = row.Used ?? "",
            ["limit"] = row.Limit ?? "",
            ["resetRule"] = string.IsNullOrEmpty(row.ResetRule) ? "never" : row.ResetRule,
            ["source"] = "manual",
        }).ToList();
        File.WriteAllText(StorePath, JsonSerializer.Serialize(payload));
    }

    public static string DefaultBlank => "";

    public static string DisplayOrBlank(string? value) =>
        string.IsNullOrWhiteSpace(value) ? DefaultBlank : value.Trim();
}
