namespace Shell;

public interface IOwnedMenuSink
{
    void SetSelectedCommandId(int commandId);
    void CompleteDeferral();
    void ScheduleDispose();
}

public sealed class OwnedMenuController
{
    public const string SubmenuTag = "submenu";

    private readonly IOwnedMenuSink sink;
    private bool finished;

    public OwnedMenuController(IOwnedMenuSink sink)
    {
        this.sink = sink;
    }

    public bool Finished => finished;
    public int? SelectedCommandId { get; private set; }

    public static bool IsSubmenuParent(object? tag, bool hasChildren) =>
        hasChildren || Equals(tag, SubmenuTag);

    public void OnItemClicked(object? tag, bool isSubmenuParent)
    {
        if (finished) return;
        if (IsSubmenuParent(tag, isSubmenuParent)) return;
        if (tag is int id)
        {
            SelectedCommandId = id;
            sink.SetSelectedCommandId(id);
        }
        Finish();
    }

    public void OnClosed()
    {
        if (!finished) Finish();
    }

    public void Finish()
    {
        if (finished) return;
        finished = true;
        sink.CompleteDeferral();
        sink.ScheduleDispose();
    }
}
