"""Real state, staged PDF output and synchronous batch-job behavior."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from powerpdf_core.jobs import CancelToken, JobCancelled, JobSpec, run_job
from powerpdf_core.outputs import OutputCommitError
from powerpdf_core.pages import PageEditSession, SourceChangedError
from powerpdf_core.settings import SCHEMA_VERSION, SettingsStore
from powerpdf_core.workspace import Workspace


def make_pdf(path: Path, count=3):
    with PdfWriter() as writer:
        for index in range(count):
            page = writer.add_blank_page(width=100 + index * 10, height=200)
            if index == 0:
                page.rotate(90)
        writer.write(path)
    return path


def test_workspace_order_dedup_targets_folder_and_move(tmp_path):
    a = make_pdf(tmp_path / "A.pdf", 1)
    b = make_pdf(tmp_path / "b.PDF", 1)
    c = make_pdf(tmp_path / "c.pdf", 1)
    text = tmp_path / "not.txt"
    text.write_text("not a PDF")
    sub = tmp_path / "sub"
    sub.mkdir()
    make_pdf(sub / "nested.pdf", 1)
    workspace = Workspace()
    assert workspace.targets(a) == [a]
    assert workspace.targets(text) == []
    assert workspace.add([c, a, c, text, sub]) == 2
    assert workspace.targets(b) == [c, a]
    assert workspace.add_folder(tmp_path) == 1
    assert workspace.queue == [c, a, b]
    assert workspace.move(2, -1) == 1
    assert workspace.queue == [c, b, a]
    workspace.move(0, -1)
    workspace.drop(None)
    assert workspace.toggle(b) is False
    assert workspace.toggle(b) is True
    assert workspace.queue == [c, a, b]
    workspace.drop(1)
    assert workspace.queue == [c, b]
    workspace.clear()
    assert workspace.targets(a) == [a]
    assert workspace.add_folder(a) == 3
    assert workspace.queue == [a, b, c]


def test_workspace_recent_limits_missing_and_descendants(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    paths = [make_pdf(old / f"{index}.pdf", 1) for index in range(35)]
    workspace = Workspace(queue=paths[:2], recent=paths + [tmp_path / "missing.pdf"])
    assert len(workspace.recent) == 30
    workspace.remember(paths[34], paths[0], old / "not.txt")
    assert workspace.recent[:2] == [paths[34], paths[0]]
    assert len(workspace.recent) == 30
    workspace.forget(paths[0])
    assert paths[0] not in workspace.recent
    new = tmp_path / "new"
    old.rename(new)
    workspace.moved(old, new)
    assert workspace.queue == [new / "0.pdf", new / "1.pdf"]
    assert workspace.recent[0] == new / "34.pdf"
    workspace.moved(new / "0.pdf", None)
    assert workspace.queue == [new / "1.pdf"]
    workspace.moved(new, None)
    assert workspace.queue == workspace.recent == []


def test_workspace_labels_newest_first_twelve():
    workspace = Workspace(card_labels=["Old", "Old"])
    for index in range(15):
        workspace.remember_label(f"Label {index}")
    workspace.remember_label("Label 10")
    workspace.remember_label("")
    assert workspace.card_labels[0] == "Label 10"
    assert len(workspace.card_labels) == len(set(workspace.card_labels)) == 12
    assert "Old" not in workspace.card_labels


def test_settings_migration_preserves_source_and_unknown_fields(tmp_path):
    legacy = tmp_path / "legacy" / "settings.json"
    legacy.parent.mkdir()
    original = {"recent": ["/a.pdf"], "theme": "gameboy", "card_labels": ["Station"], "other": {"value": 7}}
    source = json.dumps(original).encode()
    legacy.write_bytes(source)
    current = tmp_path / "new" / "settings.json"
    store = SettingsStore(current, legacy)
    assert store.load() == original | {"schema_version": SCHEMA_VERSION}
    assert legacy.read_bytes() == source
    store.update(theme="powerpdf")
    legacy.write_text(json.dumps({"theme": "do not import again"}))
    assert store.load()["theme"] == "powerpdf"
    assert store.load()["other"] == {"value": 7}


def test_settings_stale_interfaces_merge_nested_and_atomic_json(tmp_path):
    path = tmp_path / "prefs" / "settings.json"
    tui, gui = SettingsStore(path), SettingsStore(path)
    tui.load()
    gui.load()
    tui.update({"theme": "gameboy", "ui": {"tui": {"pixels": True}}})
    gui.update({"ui": {"gui": {"geometry": [20, 30, 800, 600]}}})
    tui.update(recent=["one.pdf"])
    data = json.loads(path.read_text())
    assert data["theme"] == "gameboy"
    assert data["ui"] == {"tui": {"pixels": True}, "gui": {"geometry": [20, 30, 800, 600]}}
    assert data["recent"] == ["one.pdf"]
    assert not list(path.parent.glob("*.tmp"))


def test_settings_parallel_updates_do_not_drop_fields(tmp_path):
    path = tmp_path / "settings.json"
    def update(index):
        SettingsStore(path).update({f"ui_{index}": {"value": index}})
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(update, range(12)))
    data = SettingsStore(path).load()
    assert all(data[f"ui_{index}"] == {"value": index} for index in range(12))


def test_pages_stable_ids_selection_cursor_and_history(tmp_path):
    session = PageEditSession(make_pdf(tmp_path / "source.pdf", 4))
    assert not session.edited
    session.cursor = 1
    assert session.targets() == [1]
    session.picked = {0, 2}
    assert session.targets() == [0, 2]
    session.rotate()
    assert session.turns == {0: 1, 2: 1}
    session.reorder(1)
    assert session.order == [1, 0, 3, 2]
    assert session.order[session.cursor] == 1
    session.delete()
    assert session.order == [1, 3]
    assert session.picked == set()
    assert session.undo() and session.order == [1, 0, 3, 2]
    assert session.picked == {0, 2}
    assert session.undo() and session.order == [0, 1, 2, 3]
    assert session.undo() and not session.edited
    assert session.redo() and session.turns == {0: 1, 2: 1}
    session.rotate(2)
    assert not session.can_redo


def test_pages_block_reorder_edges_and_four_turns(tmp_path):
    session = PageEditSession(make_pdf(tmp_path / "source.pdf", 4))
    session.picked = {0, 1}
    session.reorder(-1)
    assert session.order == [0, 1, 2, 3]
    assert not session.can_undo
    session.reorder(1)
    assert session.order == [2, 0, 1, 3]
    session.undo()
    for _ in range(4):
        session.rotate()
    assert session.turns == {}
    assert not session.edited


def test_pages_save_extract_additional_rotation_and_preserve_original(tmp_path):
    source = make_pdf(tmp_path / "source.pdf")
    original = source.read_bytes()
    session = PageEditSession(source)
    session.rotate(pages=[0])
    session.cursor = 2
    session.reorder(-1)
    output = session.save(tmp_path / "edited.pdf")
    reader = PdfReader(output)
    assert [float(page.mediabox.width) for page in reader.pages] == [100, 120, 110]
    assert reader.pages[0].rotation == 180
    session.picked = {0, 1}
    extracted = session.extract(tmp_path / "extract.pdf")
    selected = PdfReader(extracted)
    assert [float(page.mediabox.width) for page in selected.pages] == [100, 110]
    assert selected.pages[0].rotation == 180
    assert source.read_bytes() == original
    assert PdfReader(source).pages[0].rotation == 90
    with pytest.raises(ValueError, match="original"):
        session.save(source)
    # A second save starts from source bytes, not a previously rotated page object.
    assert PdfReader(session.save(tmp_path / "again.pdf")).pages[0].rotation == 180


def test_pages_byte_source_and_empty_document(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 1)
    session = PageEditSession(source.read_bytes())
    assert session.path is None
    assert len(PdfReader(session.save(tmp_path / "copy.pdf")).pages) == 1
    session.delete()
    assert session.targets() == []
    with pytest.raises(ValueError, match="No pages"):
        session.save(tmp_path / "empty.pdf")
    assert not (tmp_path / "empty.pdf").exists()
    assert session.undo() and session.order == [0]


def test_pages_changed_source_rejected_before_commit(tmp_path):
    source = make_pdf(tmp_path / "source.pdf")
    output = tmp_path / "existing.pdf"
    output.write_bytes(b"keep existing output")
    session = PageEditSession(source)
    def change_source(event):
        if event.completed == event.total:
            source.write_bytes(b"changed outside the app")
    with pytest.raises(OutputCommitError) as raised:
        session.save(output, progress=change_source)
    assert isinstance(raised.value.cause, SourceChangedError)
    assert raised.value.committed == ()
    assert output.read_bytes() == b"keep existing output"
    with pytest.raises(SourceChangedError):
        session.save(output)
    assert not any(path.is_dir() for path in tmp_path.iterdir())


def test_pages_cancel_before_and_during_encoding_leaves_output(tmp_path):
    session = PageEditSession(make_pdf(tmp_path / "source.pdf"))
    output = tmp_path / "existing.pdf"
    output.write_bytes(b"unchanged")
    token = CancelToken()
    token.cancel()
    with pytest.raises(JobCancelled):
        session.save(output, cancel=token)
    token = CancelToken()
    with pytest.raises(JobCancelled):
        session.save(output, cancel=token, progress=lambda event: token.cancel())
    assert output.read_bytes() == b"unchanged"


def test_jobs_per_input_failures_keep_real_outputs_and_callback(tmp_path):
    good = make_pdf(tmp_path / "good.pdf", 2)
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"this is not a PDF")
    another = make_pdf(tmp_path / "another.pdf", 1)
    observed = []
    result = run_job(JobSpec("split", [good, broken, another]), on_outcome=observed.append)
    assert not result.success and not result.cancelled
    assert tuple(observed) == result.outcomes
    assert [item.status for item in result.outcomes] == ["success", "error", "success"]
    assert [item.pages for item in result.outcomes] == [2, None, 1]
    assert result.errors[0].inputs == (broken,)
    assert len(result.outputs) == 3
    assert all(path.is_file() and len(PdfReader(path).pages) == 1 for path in result.outputs)
    assert result.output_bytes == sum(path.stat().st_size for path in result.outputs)


def test_jobs_cancel_between_inputs_keeps_completed_outcomes(tmp_path):
    paths = [make_pdf(tmp_path / f"{index}.pdf", 1) for index in range(3)]
    token = CancelToken()
    result = run_job(JobSpec("split", paths), cancel=token, on_outcome=lambda outcome: token.cancel())
    assert result.cancelled and not result.success
    assert len(result.outcomes) == len(result.outputs) == 1
    assert result.outcomes[0].success
    assert not paths[1].with_name("1_pages").exists()


def test_jobs_cancel_during_pages_is_not_an_input_error(tmp_path):
    source = make_pdf(tmp_path / "source.pdf")
    output = tmp_path / "edited.pdf"
    output.write_bytes(b"existing")
    token = CancelToken()
    def cancel_during_encode(event):
        if event.message == "pages":
            token.cancel()
    result = run_job(JobSpec("pages-save", [source], output), cancel=token, progress=cancel_during_encode)
    assert result.cancelled and not result.success
    assert result.outcomes[0].status == "cancelled"
    assert result.errors == result.outputs == ()
    assert output.read_bytes() == b"existing"


def test_jobs_merge_structured_output_and_explicit_actions(tmp_path):
    paths = [make_pdf(tmp_path / f"{index}.pdf", 1) for index in range(2)]
    output = tmp_path / "merged.pdf"
    result = run_job(JobSpec("merge", paths, output))
    assert result.success
    assert result.outputs == (output,)
    assert result.outcomes[0].inputs == tuple(paths)
    assert result.outcomes[0].pages == 2
    assert len(PdfReader(output).pages) == 2
    with pytest.raises(ValueError, match="Unknown job action"):
        run_job(JobSpec("guess what to do", paths))
    with pytest.raises(ValueError, match="exactly one input"):
        run_job(JobSpec("split", paths, tmp_path / "ambiguous"))


def test_jobs_compress_small_inputs_explicitly_skipped(tmp_path):
    source = make_pdf(tmp_path / "already_small.pdf", 1)
    result = run_job(JobSpec("compress", [source]))
    assert result.outcomes[0].status == "skipped"
    assert result.outputs == ()


def test_jobs_partial_commit_reports_only_committed_paths(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 3)
    folder = tmp_path / "source_pages"
    blocked = folder / "source_page_002.pdf"
    def block_second_output(event):
        # All pages have encoded and validation passed; simulate an external
        # writer creating an incompatible destination before the commit.
        if event.completed == 3 and event.total == 3:
            blocked.mkdir()
    result = run_job(JobSpec("split", [source]), progress=block_second_output)
    first = folder / "source_page_001.pdf"
    assert not result.success and not result.cancelled
    assert result.outputs == (first,)
    assert result.outcomes[0].status == "error"
    assert first.is_file() and blocked.is_dir()
    assert not (folder / "source_page_003.pdf").exists()
    assert result.output_bytes == first.stat().st_size


def test_jobs_extract_uses_edit_session_snapshot_state(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 3)
    session = PageEditSession(source)
    session.picked = {0, 2}
    session.rotate()
    output = tmp_path / "picked.pdf"
    result = run_job(JobSpec("pages-extract", [source], output, {"session": session}))
    assert result.success and result.outcomes[0].pages == 2
    reader = PdfReader(output)
    assert [float(page.mediabox.width) for page in reader.pages] == [100, 120]
    assert [page.rotation for page in reader.pages] == [180, 90]
