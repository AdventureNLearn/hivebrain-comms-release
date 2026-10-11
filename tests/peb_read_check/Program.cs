using Shell;

var failed = 0;

void Mutant(string name, bool ok)
{
    if (ok)
    {
        Console.WriteLine("ok " + name);
        return;
    }
    failed++;
    Console.WriteLine("FAIL " + name);
}

Mutant("x64-alloc-covers-unicode",
    PebRead.RequiredAllocSize(64) == 16
    && PebRead.UnicodeStringSize(64) == 16
    && PebRead.AllocCoversRead(PebRead.RequiredAllocSize(64), PebRead.UnicodeStringSize(64)));

Mutant("x64-old-8-byte-alloc",
    PebRead.RequiredAllocSize(64) != 8
    && !PebRead.AllocCoversRead(8, PebRead.UnicodeStringSize(64)));

Mutant("x86-alloc-covers-unicode",
    PebRead.RequiredAllocSize(32) == 8
    && PebRead.UnicodeStringSize(32) == 8
    && PebRead.AllocCoversRead(PebRead.RequiredAllocSize(32), PebRead.UnicodeStringSize(32)));

Mutant("x86-old-pointer-sized-alloc",
    PebRead.RequiredAllocSize(32) != 4
    && !PebRead.AllocCoversRead(4, PebRead.UnicodeStringSize(32)));

Mutant("pointer-reads-fit",
    PebRead.AllocCoversRead(PebRead.RequiredAllocSize(64), PebRead.PointerSize(64))
    && PebRead.AllocCoversRead(PebRead.RequiredAllocSize(32), PebRead.PointerSize(32)));

Mutant("marshal-sizeof-matches-host",
    PebRead.MarshaledUnicodeStringSize == PebRead.UnicodeStringSize(IntPtr.Size * 8)
    && PebRead.MarshaledUnicodeStringSize == PebRead.RequiredAllocSize(IntPtr.Size * 8));

Mutant("bound-max-length",
    PebRead.BoundStringBytes(32, 16) == 16
    && PebRead.BoundStringBytes(8, 32) == 8
    && PebRead.BoundStringBytes(-2, 8) == 0);

Mutant("bound-even-bytes",
    PebRead.BoundStringBytes(7, 16) == 6
    && PebRead.BoundStringBytes(1, 1) == 0
    && PebRead.BoundStringBytes(10, 9) == 8);

Mutant("bytes-read-check",
    PebRead.BytesReadEnough(16, 16)
    && PebRead.BytesReadEnough(20, 16)
    && !PebRead.BytesReadEnough(8, 16)
    && !PebRead.BytesReadEnough(-1, 8));

if (failed != 0)
{
    Console.WriteLine("failed " + failed);
    return 1;
}
Console.WriteLine("9 ok");
return 0;
