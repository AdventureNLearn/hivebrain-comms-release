using System.Collections.Concurrent;

namespace Shell;

static class Program
{
    internal static readonly string LogPath = Path.GetFullPath(
        Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", "shell-crash.log"));

    internal const int LogQueueCap = 256;

    private static readonly ConcurrentQueue<string> LogQueue = new();
    private static readonly AutoResetEvent LogPulse = new(false);
    private static readonly object LogThreadGate = new();
    private static Thread? LogThread;
    private static int droppedLogs;

    internal static int DroppedLogs => Volatile.Read(ref droppedLogs);

    internal static void Log(string message)
    {
        EnqueueLine(Stamp(message));
        EnsureLogThread();
        LogPulse.Set();
    }

    internal static void LogFatal(string message)
    {
        WriteLine(Stamp(message));
    }

    private static string Stamp(string message) =>
        DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message;

    private static void EnqueueLine(string line)
    {
        LogQueue.Enqueue(line);
        while (LogQueue.Count > LogQueueCap && LogQueue.TryDequeue(out _))
            Interlocked.Increment(ref droppedLogs);
    }

    private static void WriteLine(string line)
    {
        try { File.AppendAllText(LogPath, line + Environment.NewLine); }
        catch (Exception) { }
    }

    private static void DrainQueue()
    {
        while (LogQueue.TryDequeue(out var line))
            WriteLine(line);
    }

    private static void EnsureLogThread()
    {
        if (LogThread is not null) return;
        lock (LogThreadGate)
        {
            if (LogThread is not null) return;
            LogThread = new Thread(LogLoop)
            {
                IsBackground = true,
                Name = "hbc-log",
            };
            LogThread.Start();
        }
    }

    private static void LogLoop()
    {
        while (true)
        {
            LogPulse.WaitOne(1000);
            DrainQueue();
        }
    }

    internal static void FlushLog()
    {
        LogPulse.Set();
        var deadline = Environment.TickCount64 + 1000;
        while (!LogQueue.IsEmpty && Environment.TickCount64 < deadline)
            Thread.Sleep(10);
        DrainQueue();
    }

    [STAThread]
    static void Main()
    {
        Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
        Application.ThreadException += (_, e) => LogFatal("UI " + e.Exception);
        AppDomain.CurrentDomain.UnhandledException += (_, e) => LogFatal("DOMAIN " + e.ExceptionObject);
        TaskScheduler.UnobservedTaskException += (_, e) =>
        {
            Log("TASK " + e.Exception);
            e.SetObserved();
        };
        Application.ApplicationExit += (_, _) =>
        {
            Log("exit");
            FlushLog();
        };
        Log("start");
        ApplicationConfiguration.Initialize();
        Application.Run(new Form1());
        Log("run returned");
        FlushLog();
    }
}
