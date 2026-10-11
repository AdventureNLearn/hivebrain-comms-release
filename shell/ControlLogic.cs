using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Shell;

internal static class ControlLogic
{
    internal const int TailBytes = 65536;
    internal const int BoardCap = 50;
    internal static string StatusFile { get; private set; } = "BUILDER-STATUS.md";
    internal const string EventLogName = ".hub-events.jsonl";
    internal const string LanesName = "lanes.local.json";

    internal static readonly string[] Statuses =
    {
        "queued", "picked up", "returned",
        "DENIED", "REFUSED", "SPOOFED", "DUPLICATE", "STOPPED", "ROLLED-BACK",
    };

    private static readonly Regex Verdict = new(
        @"^(?:VERDICT|verdict):\s*(queued|picked up|returned|DENIED|REFUSED|SPOOFED|DUPLICATE|STOPPED|ROLLED-BACK)(?:\s+\S+)?\s*$",
        RegexOptions.CultureInvariant);

    private static readonly Regex Field = new(@"^([a-z][a-z0-9-]*): ?(.*)$", RegexOptions.CultureInvariant);
    private static readonly Regex FeedLine = new(@"^- (\S+) (.*)$", RegexOptions.CultureInvariant);
    private static readonly Regex Needs = new(@"^NEEDS:\s*YES\s*$", RegexOptions.CultureInvariant);
    private static readonly Regex SigLine = new(@"^sig:.*$", RegexOptions.IgnoreCase | RegexOptions.Multiline);
    private static readonly Regex Tokenish = new(@"\b(sk-[A-Za-z0-9]{8,}|bearer\s+[A-Za-z0-9._\-]{8,})\b", RegexOptions.IgnoreCase);
    private static readonly Regex UsersDir = new(@"(?:[A-Za-z]:)?[\\/]+Users[\\/]+[^\s\\/]+", RegexOptions.IgnoreCase);
    private static readonly Regex PosixHome = new(@"/home/[^\s/]+");

    internal static readonly (string Id, string Label, string Page)[] Features =
    {
        ("supergrok", "SuperGrok/Grok chat", "https://grok.com/"),
        ("imagine", "Imagine generations", "https://grok.com/imagine"),
        ("x-reads", "X reads", "https://x.com/settings"),
        ("x-posts", "X posts", "https://x.com/settings"),
        ("build-cli", "Build/grok CLI", "https://grok.com/"),
    };

    internal static readonly HashSet<string> AllowedPanes = new(StringComparer.Ordinal)
    {
        "build", "build2", "build3", "build4", "build5", "build6", "build7", "build8",
        "1", "2", "3", "4", "5", "6", "7", "8",
        "pane1", "pane2", "pane3", "pane4", "pane5", "pane6", "pane7", "pane8",
    };

    internal static IReadOnlyDictionary<string, string> Owners { get; private set; } = new Dictionary<string, string>
    {
        ["HUB-TO-BUILD.md"] = "Hub",
        ["BUILDER-TO-BUILD.md"] = "Builder",
    };

    internal static IReadOnlyDictionary<string, string> Returns { get; private set; } = new Dictionary<string, string>
    {
        ["Hub"] = "BUILD-TO-HUB.md",
        ["Builder"] = "BUILD-TO-BUILDER.md",
    };

    internal const string ConfigName = "control.local.json";
    internal const string DefaultHandoffRel = "bus";
    // Hub-side fallback only. The app reads control.local.json, not this env.
    internal const string HandoffEnv = "HBC_HANDOFF";
    internal const bool BuildClaimReady = false;
    internal const string HeldUntilBuildClaim = "held until BUILD-CLAIM";
    internal static readonly string[] WriteConfigKeys =
    {
        "write", "writable", "write_handoff", "approvals", "approvals_path", "log", "log_path", "allow_write",
    };

    internal static void BindLanes(string root)
    {
        var path = Path.Combine(root, LanesName);
        if (!File.Exists(path)) return;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            if (!doc.RootElement.TryGetProperty("lanes", out var lanes) || lanes.ValueKind != JsonValueKind.Array)
                return;
            var owners = new Dictionary<string, string>(StringComparer.Ordinal);
            var returns = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (var item in lanes.EnumerateArray())
            {
                if (item.ValueKind != JsonValueKind.Object) return;
                var owner = item.TryGetProperty("owner", out var ownerEl) ? ownerEl.GetString() ?? "" : "";
                var inbox = item.TryGetProperty("inbox", out var inboxEl) ? inboxEl.GetString() ?? "" : "";
                var outbox = item.TryGetProperty("outbox", out var outboxEl) ? outboxEl.GetString() ?? "" : "";
                if (owner.Length == 0 || inbox.Length == 0 || outbox.Length == 0) return;
                if (inbox.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0) return;
                if (outbox.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0) return;
                owners[inbox] = owner;
                returns[owner] = outbox;
            }
            if (owners.Count == 0) return;
            var status = "BUILDER-STATUS.md";
            if (doc.RootElement.TryGetProperty("status", out var statusEl))
            {
                var named = statusEl.GetString() ?? "";
                if (named.Length > 0 && named.IndexOfAny(Path.GetInvalidFileNameChars()) < 0)
                    status = named;
            }
            Owners = owners;
            Returns = returns;
            StatusFile = status;
        }
        catch (Exception)
        {
        }
    }

    internal static bool HasDotDot(string raw)
    {
        foreach (var part in raw.Replace('\\', '/').Split('/'))
            if (part == "..") return true;
        return false;
    }

    internal static string HandoffFolder(string root)
    {
        var cfg = Path.Combine(root, ConfigName);
        if (File.Exists(cfg))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(cfg));
                if (doc.RootElement.ValueKind == JsonValueKind.Object
                    && doc.RootElement.TryGetProperty("handoff", out var handoff)
                    && handoff.ValueKind == JsonValueKind.String)
                {
                    var raw = handoff.GetString() ?? "";
                    if (raw.Length > 0 && !HasDotDot(raw))
                        return Path.IsPathRooted(raw) ? Path.GetFullPath(raw) : Path.GetFullPath(Path.Combine(root, raw));
                }
            }
            catch (Exception)
            {
                // fall through to lab bus/
            }
        }
        return Path.GetFullPath(Path.Combine(root, DefaultHandoffRel));
    }

    internal static string ApprovalsPath(string root) => Path.Combine(root, "approvals.jsonl");

    internal static string HandoffLabel(string root)
    {
        var cfg = Path.Combine(root, ConfigName);
        if (File.Exists(cfg))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(cfg));
                if (doc.RootElement.ValueKind == JsonValueKind.Object
                    && doc.RootElement.TryGetProperty("handoff", out var handoff)
                    && handoff.ValueKind == JsonValueKind.String)
                {
                    var raw = handoff.GetString() ?? "";
                    if (raw.Length > 0 && HasDotDot(raw))
                        return "rejected";
                }
            }
            catch (Exception)
            {
                // fall through
            }
        }
        return DisplayHandoff(HandoffFolder(root), root);
    }

    internal static bool UnderUsersRoot(string path)
    {
        var slash = path.Replace("\\", "/");
        return Regex.IsMatch(slash, @"(?i)(?:[A-Za-z]:)?/Users/[^/]+")
            || Regex.IsMatch(slash, @"/home/[^/]+");
    }

    internal static bool UnderEtc(string path)
    {
        if (string.IsNullOrEmpty(path)) return false;
        var slash = path.Replace("\\", "/");
        return Regex.IsMatch(slash, @"(?i)^(?:[A-Za-z]:)?/etc(?:/|$)")
            || Regex.IsMatch(slash, @"(?i)(?:^|/)\.\./etc(?:/|$)");
    }

    internal static string RedactForeignUser(string shown, string path)
    {
        if (UnderEtc(path) || UnderEtc(shown)) return "outside";
        var slash = path.Replace("\\", "/");
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile).Replace("\\", "/");
        var foreign = UnderUsersRoot(path)
            || Regex.IsMatch(shown, @"(?i)Users/[^/]+")
            || Regex.IsMatch(shown, @"/home/[^/]+");
        if (!foreign) return shown;
        if (!string.IsNullOrEmpty(home) && slash.StartsWith(home, StringComparison.OrdinalIgnoreCase))
            return shown;
        return "outside";
    }

    internal static string DisplayHandoff(string path, string root)
    {
        if (UnderEtc(path)) return "outside";
        try
        {
            var rel = Path.GetRelativePath(root, path).Replace("\\", "/");
            if (!rel.StartsWith("..", StringComparison.Ordinal))
                return RedactForeignUser(string.IsNullOrEmpty(rel) ? DefaultHandoffRel : rel, path);
        }
        catch (Exception)
        {
            // tilde form next
        }
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        if (!string.IsNullOrEmpty(home))
        {
            try
            {
                var rel = Path.GetRelativePath(home, path).Replace("\\", "/");
                if (!rel.StartsWith("..", StringComparison.Ordinal))
                    return RedactForeignUser("~/" + rel.TrimStart('/'), path);
            }
            catch (Exception)
            {
                // truthful relative next
            }
        }
        try
        {
            var rel = Path.GetRelativePath(root, path).Replace("\\", "/");
            return RedactForeignUser(string.IsNullOrEmpty(rel) ? DefaultHandoffRel : rel, path);
        }
        catch (Exception)
        {
            return RedactForeignUser(path.Replace("\\", "/"), path);
        }
    }

    internal static string PlainText(string? value) => value ?? "";

    internal static bool AllowedUsageLink(string? url)
    {
        if (string.IsNullOrWhiteSpace(url)) return false;
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri)) return false;
        if (uri.Scheme != Uri.UriSchemeHttps) return false;
        var host = uri.Host;
        return host.Equals("grok.com", StringComparison.OrdinalIgnoreCase)
            || host.EndsWith(".grok.com", StringComparison.OrdinalIgnoreCase)
            || host.Equals("x.com", StringComparison.OrdinalIgnoreCase)
            || host.EndsWith(".x.com", StringComparison.OrdinalIgnoreCase);
    }

    internal static string FeatureLink(string id)
    {
        foreach (var (featureId, _, page) in Features)
            if (featureId == id) return page;
        return "";
    }

    internal static string RelativeToHandoff(string path, string handoff)
    {
        try
        {
            return Path.GetRelativePath(handoff, path).Replace("\\", "/");
        }
        catch (Exception)
        {
            return Path.GetFileName(path);
        }
    }

    internal static string ScreenSafe(string? text, string? handoff = null)
    {
        var output = PlainText(text);
        if (output.Length == 0) return "";
        output = SigLine.Replace(output, "sig: <redacted>");
        output = Tokenish.Replace(output, "<redacted>");
        if (!string.IsNullOrEmpty(handoff))
        {
            output = output.Replace(handoff, "", StringComparison.Ordinal);
            output = output.Replace(handoff.Replace("\\", "/"), "", StringComparison.Ordinal);
        }
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        if (!string.IsNullOrEmpty(home))
        {
            output = output.Replace(home, "<home>", StringComparison.Ordinal);
            output = output.Replace(home.Replace("\\", "/"), "<home>", StringComparison.Ordinal);
        }
        output = UsersDir.Replace(output, "<home>");
        output = PosixHome.Replace(output, "<home>");
        return output;
    }

    internal static string RoutedIso(string? value)
    {
        if (string.IsNullOrWhiteSpace(value)) return "";
        var parts = value.Trim().Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        for (var i = parts.Length - 1; i >= 0; i--)
        {
            if (DateTimeOffset.TryParse(parts[i], out _))
                return parts[i];
        }
        return "";
    }

    internal static string FriendlyEt(string? value)
    {
        var text = RoutedIso(value);
        if (string.IsNullOrWhiteSpace(text)) return "";
        if (!DateTimeOffset.TryParse(text, out var when)) return "";
        try
        {
            var et = TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time");
            var local = TimeZoneInfo.ConvertTime(when, et);
            return local.ToString("h:mm tt") + " ET";
        }
        catch (Exception)
        {
            return when.ToString("h:mm tt") + " ET";
        }
    }

    internal static string AgeLabel(DateTimeOffset readAt, DateTimeOffset now)
    {
        var seconds = (int)Math.Max(0, (now - readAt).TotalSeconds);
        if (seconds < 60) return seconds + " s ago";
        var minutes = seconds / 60;
        if (minutes < 60) return minutes + " min ago";
        return (minutes / 60) + " h ago";
    }

    internal static string? VerdictStatus(IReadOnlyList<string> lines)
    {
        string? found = null;
        foreach (var line in lines)
        {
            var match = Verdict.Match(line.Trim());
            if (match.Success) found = match.Groups[1].Value;
        }
        return found;
    }

    internal static List<string[]> Blocks(string text)
    {
        var lines = Regex.Split(text.Replace("\r\n", "\n").Replace('\r', '\n'), "\n");
        var found = new List<string[]>();
        var index = 0;
        while (index < lines.Length)
        {
            if (lines[index].StartsWith("id: ", StringComparison.Ordinal))
            {
                var start = index;
                index += 1;
                while (index < lines.Length &&
                       !lines[index].StartsWith("id: ", StringComparison.Ordinal) &&
                       !lines[index].StartsWith("## ", StringComparison.Ordinal))
                    index += 1;
                var block = new string[index - start];
                Array.Copy(lines, start, block, 0, block.Length);
                found.Add(block);
            }
            else index += 1;
        }
        return found;
    }

    internal static Dictionary<string, string> FieldsOf(IReadOnlyList<string> block)
    {
        var fields = new Dictionary<string, string>(StringComparer.Ordinal);
        string? current = null;
        foreach (var line in block)
        {
            var stripped = line.Trim();
            if (Verdict.IsMatch(stripped) || Needs.IsMatch(stripped))
            {
                current = null;
                continue;
            }
            var match = Field.Match(line);
            if (match.Success)
            {
                current = match.Groups[1].Value;
                fields[current] = match.Groups[2].Value;
            }
            else if (current is not null)
                fields[current] = (fields[current] + " " + line).Trim();
        }
        return fields;
    }

    internal static string BlockId(IReadOnlyList<string> block, IReadOnlyDictionary<string, string>? fields = null)
    {
        foreach (var line in block)
        {
            if (line.StartsWith("id: ", StringComparison.Ordinal))
                return line.Substring(4).Trim();
        }
        if (fields is not null && fields.TryGetValue("id", out var id))
            return id.Trim();
        return "";
    }

    internal static string ReadFile(string path)
    {
        try
        {
            if (!File.Exists(path)) return "";
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            var length = stream.Length;
            var take = (int)Math.Min(length, TailBytes);
            if (length > take) stream.Seek(length - take, SeekOrigin.Begin);
            var buffer = new byte[take];
            var read = stream.Read(buffer, 0, take);
            var text = Encoding.UTF8.GetString(buffer, 0, read);
            if (length > TailBytes)
            {
                var newline = text.IndexOf('\n');
                if (newline >= 0) text = text[(newline + 1)..];
            }
            return text;
        }
        catch (Exception)
        {
            return "";
        }
    }

    internal sealed class JobRow
    {
        public string Id = "";
        public string Sender = "";
        public string Routed = "";
        public string RoutedEt = "";
        public string Status = "";
        public string ReturnFile = "";
        public string ReturnText = "";
    }

    internal sealed class RefreshSnapshot
    {
        public List<JobRow> Jobs = new();
        public List<NeedRow> Ready = new();
    }

    internal static RefreshSnapshot LoadRefresh(string handoff, string? approvals = null, int limit = BoardCap)
    {
        var approvalsPath = approvals ?? Path.Combine(Path.GetDirectoryName(handoff) ?? "", "approvals.jsonl");
        var history = ApprovalsHistoryFromText(ReadFile(approvalsPath));
        var state = ApprovalsStateFromHistory(history);
        var jobs = JobBoardFromState(handoff, state, history, limit);
        return new RefreshSnapshot
        {
            Jobs = jobs,
            Ready = ReadyFromJobs(jobs, state),
        };
    }

    internal static List<JobRow> JobBoard(string handoff, string? approvals = null, int limit = BoardCap) =>
        LoadRefresh(handoff, approvals, limit).Jobs;

    private static List<JobRow> JobBoardFromState(
        string handoff,
        IReadOnlyDictionary<string, ApprovalRow> state,
        IReadOnlyDictionary<string, List<ApprovalRow>> history,
        int limit)
    {
        var returns = ReturnBlocks(handoff);
        var rows = new List<JobRow>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var (name, owner) in Owners)
        {
            foreach (var block in Blocks(ReadFile(Path.Combine(handoff, name))))
            {
                var fields = FieldsOf(block);
                var id = BlockId(block, fields);
                if (id.Length == 0 || !seen.Add(id))
                    continue;
                returns.TryGetValue(id, out var back);
                state.TryGetValue(id, out var approval);
                history.TryGetValue(id, out var hist);
                var routed = fields.TryGetValue("routed", out var routedRaw) ? routedRaw.Trim() : "";
                rows.Add(new JobRow
                {
                    Id = id,
                    Sender = owner,
                    Routed = routed,
                    RoutedEt = FriendlyEt(routed),
                    Status = MappedStatus(back?.Lines, approval, hist),
                    ReturnFile = Returns[owner],
                    ReturnText = back is null ? "" : ScreenSafe(string.Join("\n", back.Lines), handoff),
                });
            }
        }
        rows.Sort((a, b) => RoutedKey(a.Routed).CompareTo(RoutedKey(b.Routed)));
        if (rows.Count > limit) rows = rows.GetRange(rows.Count - limit, limit);
        return rows;
    }

    internal static string MappedStatus(IReadOnlyList<string>? returnLines, ApprovalRow? approval, IReadOnlyList<ApprovalRow>? history = null)
    {
        var rows = new List<ApprovalRow>();
        if (history is not null) rows.AddRange(history);
        else if (approval is not null) rows.Add(approval);
        var approved = rows.Exists(row => row.Decision == "approve");
        var returnExists = returnLines is not null;
        var ran = approved || returnExists;
        string? retVerdict = returnLines is not null ? VerdictStatus(returnLines) : null;
        string? approvalVerdict = null;
        foreach (var row in rows)
        {
            if (ran && (row.Decision == "held" || row.Status == "DUPLICATE"))
                continue;
            if (!string.IsNullOrEmpty(row.Status))
                approvalVerdict = row.Status;
        }
        if (!string.IsNullOrEmpty(retVerdict)) return retVerdict;
        if (!string.IsNullOrEmpty(approvalVerdict)) return approvalVerdict;
        if (approved && returnExists) return "returned";
        if (approved) return "picked up";
        if (returnExists) return "returned";
        return "queued";
    }

    private static DateTimeOffset RoutedKey(string routed)
    {
        var iso = RoutedIso(routed);
        return DateTimeOffset.TryParse(iso, out var when) ? when : DateTimeOffset.MinValue;
    }

    internal sealed class ApprovalRow
    {
        public string Decision = "";
        public string Status = "";
        public string Reason = "";
    }

    internal static Dictionary<string, List<ApprovalRow>> ApprovalsHistory(string path) =>
        ApprovalsHistoryFromText(ReadFile(path));

    internal static Dictionary<string, List<ApprovalRow>> ApprovalsHistoryFromText(string text)
    {
        var found = new Dictionary<string, List<ApprovalRow>>(StringComparer.Ordinal);
        foreach (var line in text.Split('\n'))
        {
            var trimmed = line.Trim();
            if (trimmed.Length == 0) continue;
            try
            {
                using var doc = JsonDocument.Parse(trimmed);
                if (doc.RootElement.ValueKind != JsonValueKind.Object) continue;
                var id = doc.RootElement.TryGetProperty("id", out var idEl) ? idEl.GetString() ?? "" : "";
                if (id.Length == 0) continue;
                if (!found.TryGetValue(id, out var list))
                {
                    list = new List<ApprovalRow>();
                    found[id] = list;
                }
                list.Add(new ApprovalRow
                {
                    Decision = doc.RootElement.TryGetProperty("decision", out var decEl) ? decEl.GetString() ?? "" : "",
                    Reason = doc.RootElement.TryGetProperty("reason", out var reasonEl) ? reasonEl.GetString() ?? "" : "",
                    Status = ApprovalStatus(doc.RootElement),
                });
            }
            catch (Exception)
            {
                // skip
            }
        }
        return found;
    }

    internal static Dictionary<string, ApprovalRow> ApprovalsState(string path) =>
        ApprovalsStateFromHistory(ApprovalsHistory(path));

    internal static Dictionary<string, ApprovalRow> ApprovalsStateFromHistory(
        IReadOnlyDictionary<string, List<ApprovalRow>> history)
    {
        var found = new Dictionary<string, ApprovalRow>(StringComparer.Ordinal);
        foreach (var (id, rows) in history)
        {
            if (rows.Count > 0) found[id] = rows[^1];
        }
        return found;
    }

    internal static List<NeedRow> HeldFromApprovals(string path)
    {
        var rows = new List<NeedRow>();
        foreach (var (id, row) in ApprovalsState(path))
        {
            if (row.Decision != "held") continue;
            rows.Add(new NeedRow { Id = id, Reason = row.Reason.Length > 0 ? row.Reason : "held" });
        }
        return rows;
    }

    private static string ApprovalStatus(JsonElement row)
    {
        var decision = row.TryGetProperty("decision", out var decEl) ? decEl.GetString() ?? "" : "";
        var reason = row.TryGetProperty("reason", out var reasonEl) ? reasonEl.GetString() ?? "" : "";
        if (Statuses.Contains(decision)) return decision;
        foreach (var token in Statuses)
        {
            if (reason == token || reason.StartsWith(token + " ", StringComparison.Ordinal))
                return token;
        }
        if (decision == "deny") return "DENIED";
        return "";
    }

    private sealed class ReturnBlock
    {
        public string[] Lines = Array.Empty<string>();
    }

    private static Dictionary<string, ReturnBlock> ReturnBlocks(string handoff)
    {
        var byId = new Dictionary<string, ReturnBlock>(StringComparer.Ordinal);
        foreach (var (owner, name) in Returns)
        {
            foreach (var block in Blocks(ReadFile(Path.Combine(handoff, name))))
            {
                var fields = FieldsOf(block);
                var id = BlockId(block, fields);
                if (id.Length == 0) continue;
                byId[id] = new ReturnBlock { Lines = block };
            }
        }
        return byId;
    }

    internal static string JobReturnDisplay(string handoff, string jobId)
    {
        var returns = ReturnBlocks(handoff);
        return returns.TryGetValue(jobId, out var back)
            ? ScreenSafe(string.Join("\n", back.Lines), handoff)
            : "";
    }

    internal sealed class NeedRow
    {
        public string Id = "";
        public string Reason = "";
    }

    internal static List<NeedRow> ReadyQueue(string handoff, string? approvals = null) =>
        LoadRefresh(handoff, approvals).Ready;

    internal static List<NeedRow> ReadyFromJobs(
        IEnumerable<JobRow> jobs,
        IReadOnlyDictionary<string, ApprovalRow> state)
    {
        var held = new HashSet<string>(StringComparer.Ordinal);
        foreach (var (id, row) in state)
            if (row.Decision == "held" && id.Length > 0) held.Add(id);
        var rows = new List<NeedRow>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var row in jobs)
        {
            if (row.Status != "queued") continue;
            if (row.Id.Length == 0 || !seen.Add(row.Id) || held.Contains(row.Id)) continue;
            rows.Add(new NeedRow { Id = row.Id, Reason = "ready" });
        }
        return rows;
    }

    internal static List<NeedRow> NeedsYou(IEnumerable<NeedRow>? ready)
    {
        var rows = new List<NeedRow>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in ready ?? Array.Empty<NeedRow>())
        {
            var id = (item.Id ?? "").Trim();
            if (id.Length == 0 || !seen.Add(id)) continue;
            rows.Add(new NeedRow { Id = id, Reason = string.IsNullOrEmpty(item.Reason) ? "ready" : item.Reason });
        }
        return rows;
    }

    internal sealed class ClaimStateRow
    {
        public string Id = "";
        public bool Claimed;
        public bool Returned;
    }

    internal static ClaimStateRow? ClaimState()
    {
        if (!BuildClaimReady) return null;
        return null;
    }

    internal static JobRow? RunningJob(string handoff, string? approvals = null)
    {
        var claim = ClaimState();
        if (claim is null) return null;
        if (claim.Claimed && !claim.Returned)
            return new JobRow { Id = claim.Id, Status = "claimed" };
        return JobBoard(handoff, approvals).Find(row => row.Status == "picked up");
    }

    internal sealed class FeedRow
    {
        public string Time = "";
        public string TimeEt = "";
        public string Text = "";
        public string File = StatusFile;
    }

    internal static List<FeedRow> StatusFeed(string handoff, int limit = 30)
    {
        var path = Path.Combine(handoff, StatusFile);
        var found = new List<FeedRow>();
        foreach (var line in ReadFile(path).Split('\n'))
        {
            var match = FeedLine.Match(line.TrimEnd('\r'));
            if (!match.Success) continue;
            found.Add(new FeedRow
            {
                Time = match.Groups[1].Value,
                TimeEt = FriendlyEt(match.Groups[1].Value),
                Text = ScreenSafe(match.Groups[2].Value, handoff),
                File = RelativeToHandoff(path, handoff),
            });
        }
        if (found.Count > limit) found = found.GetRange(found.Count - limit, limit);
        return found;
    }

    internal sealed class FeedSnapshot
    {
        public List<FeedRow> Rows = new();
        public bool FileFound;
    }

    internal static FeedSnapshot LoadFeed(string handoff, int limit = 30)
    {
        var path = Path.Combine(handoff, StatusFile);
        var found = false;
        try { found = File.Exists(path); }
        catch (Exception) { }
        return new FeedSnapshot { Rows = StatusFeed(handoff, limit), FileFound = found };
    }

    internal sealed class EventRow
    {
        public string Time = "";
        public string Event = "";
        public string Pane = "";
        public string Exit = "";
    }

    internal static List<EventRow> ReadEvents(string root, int limit = 10)
    {
        var path = Path.Combine(root, EventLogName);
        var found = new List<EventRow>();
        foreach (var line in ReadFile(path).Split('\n'))
        {
            var trimmed = line.Trim();
            if (trimmed.Length == 0) continue;
            try
            {
                using var doc = JsonDocument.Parse(trimmed);
                if (doc.RootElement.ValueKind != JsonValueKind.Object) continue;
                var ev = doc.RootElement.TryGetProperty("event", out var evEl) ? evEl.GetString() ?? "" : "";
                if (ev != "hub restart" && ev != "build exit") continue;
                var pane = doc.RootElement.TryGetProperty("pane", out var paneEl) ? paneEl.GetString() ?? "" : "";
                if (pane.Length > 0 && !AllowedPanes.Contains(pane)) pane = "";
                var exit = "";
                if (doc.RootElement.TryGetProperty("exit", out var exitEl) && exitEl.ValueKind == JsonValueKind.Number
                    && exitEl.TryGetInt32(out var exitInt) && exitInt >= -1048576 && exitInt <= 1048576)
                    exit = exitInt.ToString();
                found.Add(new EventRow
                {
                    Time = doc.RootElement.TryGetProperty("time", out var timeEl) ? timeEl.GetString() ?? "" : "",
                    Event = ev,
                    Pane = pane,
                    Exit = exit,
                });
            }
            catch (Exception)
            {
                // skip a bad line; do not blank the strip
            }
        }
        if (found.Count > limit) found = found.GetRange(found.Count - limit, limit);
        return found;
    }

    internal sealed class UsageRow
    {
        public string Id = "";
        public string Label = "";
        public string Used = "UNKNOWN";
        public string Limit = "UNKNOWN";
        public string Reset = "UNKNOWN";
        public string Source = "none";
        public string ReadAt = "UNKNOWN";
        public string Age = "";
        public string UsagePage = "";
    }

    internal static List<UsageRow> UsageRows()
    {
        var saved = UsageStore.Load();
        var readAt = DateTimeOffset.Now.ToString("o");
        var rows = new List<UsageRow>();
        foreach (var (id, label, page) in Features)
        {
            saved.TryGetValue(id, out var row);
            var used = UsageStore.DisplayOrBlank(row?.Used);
            var limit = UsageStore.DisplayOrBlank(row?.Limit);
            rows.Add(new UsageRow
            {
                Id = id,
                Label = label,
                Used = used.Length == 0 ? "UNKNOWN" : used,
                Limit = limit.Length == 0 ? "UNKNOWN" : limit,
                Reset = "UNKNOWN",
                Source = row is null ? "none" : row.Source,
                ReadAt = row is null ? "UNKNOWN" : readAt,
                UsagePage = page,
            });
        }
        return rows;
    }

    internal static void LogAction(string logPath, string action, string result, string extraJson = "")
    {
        var time = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
        var row = extraJson.Length == 0
            ? $"{{\"time\":\"{time}\",\"action\":\"{action}\",\"result\":\"{result}\"}}"
            : $"{{\"time\":\"{time}\",\"action\":\"{action}\",\"result\":\"{result}\",{extraJson.Trim().TrimStart(',')}}}";
        File.AppendAllText(logPath, row + Environment.NewLine);
    }
}

internal interface IProcessManager
{
    Task<bool> HubRunning();
    Task StartHub();
    Task FocusBuildTab();
    bool HasRestartRoute();
    Task<bool> RestartBuildPane();
    ControlLogic.JobRow? RunningJob();
    Task<bool> WaitUntilBuildIdle();
}

internal static class ControlActions
{
    internal static async Task RestartBuildOnly(IProcessManager manager, string logPath, Func<string, bool> confirm)
    {
        if (!ControlLogic.BuildClaimReady)
        {
            ControlLogic.LogAction(logPath, "restart-build", "disabled", "\"reason\":\"held until BUILD-CLAIM\"");
            return;
        }
        if (!manager.HasRestartRoute())
        {
            ControlLogic.LogAction(logPath, "restart-build", "disabled", "\"reason\":\"no route\"");
            return;
        }
        var claim = ControlLogic.ClaimState();
        if (claim is null)
        {
            ControlLogic.LogAction(logPath, "restart-build", "disabled", "\"reason\":\"held until BUILD-CLAIM\"");
            return;
        }
        var job = (claim.Claimed && !claim.Returned)
            ? new ControlLogic.JobRow { Id = claim.Id, Status = "claimed" }
            : manager.RunningJob();
        if (job is not null)
        {
            if (!confirm("Job " + job.Id + " is picked up. Wait for it to finish, then restart Build only?"))
            {
                ControlLogic.LogAction(logPath, "restart-build", "cancelled", "\"reason\":\"confirm\"");
                return;
            }
            var idle = await manager.WaitUntilBuildIdle();
            if (!idle)
            {
                var still = manager.RunningJob() ?? job;
                if (!confirm("Job " + still.Id + " is still picked up. Restart Build only anyway?"))
                {
                    ControlLogic.LogAction(logPath, "restart-build", "cancelled", "\"reason\":\"still running\"");
                    return;
                }
            }
        }
        else if (!confirm("Restart the hub Build pane only? Hub, approvals, and the app stay up."))
        {
            ControlLogic.LogAction(logPath, "restart-build", "cancelled", "\"reason\":\"confirm\"");
            return;
        }
        if (!await manager.RestartBuildPane())
        {
            ControlLogic.LogAction(logPath, "restart-build", "error", "\"reason\":\"pane\"");
            return;
        }
        ControlLogic.LogAction(logPath, "restart-build", "ok");
    }

    internal static async Task StartAll(IProcessManager manager, string logPath, Func<string, bool> confirm)
    {
        if (!confirm("Start the hub if it is down, and open the Build tab?"))
        {
            ControlLogic.LogAction(logPath, "start-all", "cancelled", "\"reason\":\"confirm\"");
            return;
        }
        var started = new List<string>();
        if (!await manager.HubRunning()) { await manager.StartHub(); started.Add("hub"); }
        await manager.FocusBuildTab();
        var extra = "\"started\":[" + string.Join(",", started.Select(name => "\"" + name + "\"")) + "],\"build\":\"tab\"";
        ControlLogic.LogAction(logPath, "start-all", "ok", extra);
    }
}
