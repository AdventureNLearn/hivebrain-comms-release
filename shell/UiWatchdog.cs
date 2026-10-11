namespace Shell;

internal static class UiWatchdog
{
    internal const int PingMs = 1000;
    internal const int HangMs = 4000;

    private static int started;

    internal static void Start(Control ui)
    {
        if (Interlocked.Exchange(ref started, 1) != 0) return;
        long lastPump = Environment.TickCount64;
        var hanging = false;
        void Ping() => Volatile.Write(ref lastPump, Environment.TickCount64);
        var thread = new Thread(() =>
        {
            while (!ui.IsDisposed)
            {
                try
                {
                    if (ui.IsHandleCreated)
                        ui.BeginInvoke((Action)Ping);
                }
                catch (ObjectDisposedException)
                {
                    break;
                }
                catch (InvalidOperationException)
                {
                }

                Thread.Sleep(PingMs);
                var lag = Environment.TickCount64 - Volatile.Read(ref lastPump);
                if (lag >= HangMs)
                {
                    if (!hanging)
                    {
                        hanging = true;
                        Program.Log("UI hang suspected, " + (lag / 1000) + "s");
                    }
                }
                else hanging = false;
            }
        })
        {
            IsBackground = true,
            Name = "hbc-ui-watchdog",
        };
        thread.Start();
    }
}
