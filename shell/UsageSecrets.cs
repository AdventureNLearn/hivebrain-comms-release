using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace Shell;

/// <summary>
/// Optional X API bearer. Credential Manager or DPAPI file only. Never logged.
/// </summary>
internal static class UsageSecrets
{
    public const string CredTarget = "LPIN/XApiBearer";
    public const string ProtectedFileName = "x-api-bearer.dpapi";

    public static string ReadXBearer(string appDataDir)
    {
        try
        {
            if (!OperatingSystem.IsWindows())
                return "";
            var cred = ReadCredentialManager();
            if (cred.Length > 0) return cred;
            var path = Path.Combine(appDataDir, ProtectedFileName);
            if (!File.Exists(path)) return "";
            var blob = File.ReadAllBytes(path);
            if (blob.Length == 0) return "";
            var plain = ProtectedData.Unprotect(blob, null, DataProtectionScope.CurrentUser);
            return Encoding.UTF8.GetString(plain).Trim();
        }
        catch (Exception)
        {
            return "";
        }
    }

    static string ReadCredentialManager()
    {
        if (!CredRead(CredTarget, 1, 0, out var ptr) || ptr == IntPtr.Zero)
            return "";
        try
        {
            var cred = Marshal.PtrToStructure<NativeCred>(ptr);
            if (cred.CredentialBlob == IntPtr.Zero || cred.CredentialBlobSize <= 0)
                return "";
            return Marshal.PtrToStringUni(cred.CredentialBlob, (int)cred.CredentialBlobSize / 2)?.Trim() ?? "";
        }
        catch (Exception)
        {
            return "";
        }
        finally
        {
            CredFree(ptr);
        }
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct NativeCred
    {
        public uint Flags;
        public uint Type;
        public string TargetName;
        public string Comment;
        public long LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public string TargetAlias;
        public string UserName;
    }

    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CredRead(string target, uint type, uint flags, out IntPtr credential);

    [DllImport("advapi32.dll", SetLastError = true)]
    static extern void CredFree(IntPtr buffer);
}
