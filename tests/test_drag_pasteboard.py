"""The Mac drag pasteboard keeps the *last* file drag forever. Only trust it when a new drag
has written to it since we last looked (its change count moved)."""
from dragshift.platform_mac import DragWatch


def reader(paths):
    calls = []

    def read():
        calls.append(1)
        return list(paths)
    return read, calls


def test_new_drag_is_reported():
    w = DragWatch()
    read, _ = reader(["/Users/me/clip.mp4"])
    assert w.fresh(5, read) == ["/Users/me/clip.mp4"]


def test_same_drag_count_is_stale():
    """A Shift+drag of text, a browser tab or a remote-desktop file writes nothing to the
    pasteboard, so the count hasn't moved: the old video must not show up again."""
    w = DragWatch()
    read, calls = reader(["/Users/me/clip.mp4"])
    w.fresh(5, read)
    assert w.fresh(5, read) == []
    assert len(calls) == 1          # not even read the second time


def test_leftover_from_before_start_is_ignored():
    w = DragWatch()
    w.remember(9)                   # what was on the pasteboard when the app started
    read, _ = reader(["/Users/me/old.pdf"])
    assert w.fresh(9, read) == []
    assert w.fresh(10, read) == ["/Users/me/old.pdf"]   # a real new drag of the same file


def test_every_new_drag_counts():
    w = DragWatch()
    a, _ = reader(["/a.png"])
    b, _ = reader(["/b.pdf"])
    assert w.fresh(1, a) == ["/a.png"]
    assert w.fresh(2, b) == ["/b.pdf"]
    assert w.fresh(2, b) == []
