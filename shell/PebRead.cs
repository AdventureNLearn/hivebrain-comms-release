using System.Runtime.InteropServices;
using System.Text;

namespace Shell;

public static class PebRead
{
    public const int ProcessQueryInformation = 0x0400;
    public const int ProcessVmRead = 0x0010;
    public const int ProcessReadAccess = ProcessQueryInformation | ProcessVmRead;

    [StructLayout(LayoutKind.Sequential)]
    public struct UnicodeString
    {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessBasicInformation
    {
        public IntPtr Reserved1;
        public IntPtr PebBaseAddress;
        public IntPtr Reserved2_0;
        public IntPtr Reserved2_1;
        public IntPtr UniqueProcessId;
        public IntPtr InheritedFromUniqueProcessId;
    }

    public static int PointerSize(int bits)
    {
        if (bits != 32 && bits != 64)
            throw new ArgumentOutOfRangeException(nameof(bits));
        return bits / 8;
    }

    public static int UnicodeStringSize(int bits)
    {
        // Sequential UNICODE_STRING: ushort Length, ushort MaximumLength, IntPtr Buffer.
        // x86 is 8 bytes. x64 inserts 4 bytes of pad before Buffer, so 16.
        return bits == 64 ? 16 : 8;
    }

    public static int RequiredAllocSize(int bits) =>
        Math.Max(PointerSize(bits), UnicodeStringSize(bits));

    public static bool AllocCoversRead(int allocBytes, int readBytes) =>
        allocBytes >= readBytes && readBytes > 0;

    public static int BoundStringBytes(int length, int maximumLength)
    {
        if (length < 0) length = 0;
        if (maximumLength < 0) maximumLength = 0;
        if (length > maximumLength) length = maximumLength;
        return length & ~1;
    }

    public static bool BytesReadEnough(long bytesRead, int requested) =>
        requested >= 0 && bytesRead >= requested;

    public static int MarshaledUnicodeStringSize => Marshal.SizeOf<UnicodeString>();

    public static string CommandLine(int pid) => ReadUnicode(pid, 0x70);

    public static string CurrentDirectory(int pid) => ReadUnicode(pid, 0x38);

    public static string ReadUnicode(int pid, int offset)
    {
        var bits = IntPtr.Size * 8;
        var allocSize = RequiredAllocSize(bits);
        var pointerSize = PointerSize(bits);
        var unicodeSize = UnicodeStringSize(bits);
        var handle = OpenProcess(ProcessReadAccess, false, pid);
        if (handle == IntPtr.Zero) return "";
        try
        {
            var pbi = new ProcessBasicInformation();
            var status = NtQueryInformationProcess(
                handle, 0, ref pbi, Marshal.SizeOf<ProcessBasicInformation>(), out _);
            if (status != 0) return "";
            var block = Marshal.AllocHGlobal(allocSize);
            try
            {
                if (!TryRead(handle, pbi.PebBaseAddress + 0x20, block, pointerSize))
                    return "";
                var parameters = Marshal.ReadIntPtr(block);
                if (parameters == IntPtr.Zero) return "";
                if (!TryRead(handle, parameters + offset, block, unicodeSize))
                    return "";
                var us = Marshal.PtrToStructure<UnicodeString>(block);
                var bytes = BoundStringBytes(us.Length, us.MaximumLength);
                if (bytes == 0 || us.Buffer == IntPtr.Zero) return "";
                var data = new byte[bytes];
                var pinned = GCHandle.Alloc(data, GCHandleType.Pinned);
                try
                {
                    if (!TryRead(handle, us.Buffer, pinned.AddrOfPinnedObject(), bytes))
                        return "";
                }
                finally { pinned.Free(); }
                return Encoding.Unicode.GetString(data);
            }
            finally { Marshal.FreeHGlobal(block); }
        }
        finally { CloseHandle(handle); }
    }

    private static bool TryRead(IntPtr process, IntPtr address, IntPtr buffer, int size)
    {
        if (size <= 0 || address == IntPtr.Zero || buffer == IntPtr.Zero) return false;
        if (!ReadProcessMemory(process, address, buffer, size, out var read)) return false;
        return BytesReadEnough(read.ToInt64(), size);
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(int access, bool inherit, int pid);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("ntdll.dll", SetLastError = true)]
    private static extern int NtQueryInformationProcess(
        IntPtr process,
        int infoClass,
        ref ProcessBasicInformation info,
        int length,
        out int returnLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ReadProcessMemory(
        IntPtr process,
        IntPtr address,
        IntPtr buffer,
        int size,
        out IntPtr read);
}
