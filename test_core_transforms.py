"""Shared-transform edge cases; no application imports or installed helpers required."""
from __future__ import annotations

import subprocess
import sys
import shutil
import threading
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from powerpdf_core import compress, files, images, merge, outputs, split
from powerpdf_core.jobs import CancelToken, JobCancelled


def make_pdf(path: Path, widths=(120, 240), *, metadata=True) -> Path:
    with PdfWriter() as writer:
        for width in widths:
            writer.add_blank_page(width=width, height=180)
        if metadata:
            writer.add_metadata({"/Title": "private title", "/Author": "private author"})
            xmp = DecodedStreamObject()
            xmp.set_data(b'<x:xmpmeta xmlns:x="adobe:ns:meta/">private</x:xmpmeta>')
            xmp[NameObject("/Type")] = NameObject("/Metadata")
            xmp[NameObject("/Subtype")] = NameObject("/XML")
            writer._root_object[NameObject("/Metadata")] = writer._add_object(xmp)
        writer.write(path)
    return path


def test_discovery_sorting_differs_from_ordered_merge_expansion(tmp_path):
    a = make_pdf(tmp_path / "a.pdf")
    b = make_pdf(tmp_path / "b.PDF")
    output = make_pdf(tmp_path / "out.pdf")
    assert files.find_pdfs([b, a, b]) == [a, b]
    assert files.expand([b, a, b, output], output) == [b, a, b]
    assert files.expand([tmp_path], output) == [a, b]


def test_validate_output_rejects_alias_and_hardlink_to_source(tmp_path):
    source = make_pdf(tmp_path / "source.pdf")
    with pytest.raises(ValueError, match="input"):
        files.validate_output(tmp_path / "." / source.name, [source])
    alias = tmp_path / "alias.pdf"
    alias.hardlink_to(source)
    with pytest.raises(ValueError, match="input"):
        files.validate_output(alias, [source])


def test_split_brackets_only_remove_numeric_literal_outputs(tmp_path):
    source = make_pdf(tmp_path / "scan[1].pdf", (100,))
    folder = tmp_path / "scan[1]_pages"
    folder.mkdir()
    stale = folder / "scan[1]_page_009.pdf"
    stale.write_bytes(b"obsolete")
    unrelated = [folder / "scan1_page_009.pdf", folder / "scan[1]_page_notes.pdf",
                 folder / "scan[1]_page_002.pdf.backup"]
    for path in unrelated:
        path.write_bytes(b"keep")
    count, actual = split.split_pdf(source)
    assert (count, actual) == (1, folder)
    assert not stale.exists()
    assert all(path.read_bytes() == b"keep" for path in unrelated)
    assert len(PdfReader(folder / "scan[1]_page_001.pdf").pages) == 1


def test_split_cancel_after_last_page_does_not_commit(tmp_path):
    source = make_pdf(tmp_path / "source.pdf")
    folder = tmp_path / "source_pages"
    folder.mkdir()
    old = folder / "source_page_001.pdf"
    old.write_bytes(b"previous output")
    token = CancelToken()
    with pytest.raises(JobCancelled):
        split.split_pdf(source, cancel=token, progress=lambda event: token.cancel())
    assert old.read_bytes() == b"previous output"
    assert sorted(path.name for path in folder.iterdir()) == [old.name]


def test_merge_preserves_page_and_argument_order(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", (100, 110))
    b = make_pdf(tmp_path / "b.pdf", (200,))
    destination = tmp_path / "merged.pdf"
    assert merge.merge_pdfs([b, a], destination) == 3
    assert [int(page.mediabox.width) for page in PdfReader(destination).pages] == [200, 100, 110]


def test_merge_writer_failure_keeps_existing_destination(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf")
    destination = tmp_path / "merged.pdf"
    destination.write_bytes(b"previous output")

    def fail(self, stream):
        stream.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(PdfWriter, "write", fail)
    with pytest.raises(OSError, match="disk full"):
        merge.merge_pdfs([source], destination)
    assert destination.read_bytes() == b"previous output"
    assert not list(tmp_path.glob(".powerpdf_*"))


def test_merge_cancel_after_append_keeps_existing_destination(tmp_path):
    source = make_pdf(tmp_path / "source.pdf")
    destination = tmp_path / "merged.pdf"
    destination.write_bytes(b"previous")
    token = CancelToken()
    with pytest.raises(JobCancelled):
        merge.merge_pdfs([source], destination, cancel=token, progress=lambda event: token.cancel())
    assert destination.read_bytes() == b"previous"


def test_merge_encrypted_input_preserves_existing_output(tmp_path):
    source = tmp_path / "encrypted.pdf"
    with PdfWriter() as writer:
        writer.add_blank_page(width=100, height=100)
        writer.encrypt("secret")
        writer.write(source)
    destination = tmp_path / "output.pdf"
    destination.write_bytes(b"previous")
    with pytest.raises(ValueError, match="password-protected"):
        merge.merge_pdfs([source], destination)
    assert destination.read_bytes() == b"previous"


def test_atomic_write_cancelled_by_encoder_never_replaces(tmp_path):
    target = tmp_path / "target.pdf"
    target.write_bytes(b"old")
    token = CancelToken()

    def encode(staged):
        staged.write_bytes(b"new")
        token.cancel()

    with pytest.raises(JobCancelled):
        outputs.atomic_write(target, encode, cancel=token)
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".powerpdf_*"))


def test_commit_failure_reports_exact_partial_outputs(tmp_path, monkeypatch):
    staged = [tmp_path / "one.tmp", tmp_path / "two.tmp"]
    targets = [tmp_path / "one.pdf", tmp_path / "two.pdf"]
    for path in staged:
        path.write_bytes(b"new")
    for path in targets:
        path.write_bytes(b"old")
    stale = tmp_path / "stale.pdf"
    stale.write_bytes(b"stale")
    replace = Path.replace

    def fail_second(self, destination):
        if self == staged[1]:
            raise PermissionError("locked")
        return replace(self, destination)

    monkeypatch.setattr(Path, "replace", fail_second)
    with pytest.raises(outputs.OutputCommitError) as caught:
        outputs.commit_files(zip(staged, targets), remove=[stale])
    assert caught.value.committed == (targets[0],)
    assert caught.value.removed == ()
    assert targets[0].read_bytes() == b"new"
    assert targets[1].read_bytes() == b"old"
    assert stale.exists()


def test_cancel_during_commit_reports_replacements_already_made(tmp_path, monkeypatch):
    staged = [tmp_path / "one.tmp", tmp_path / "two.tmp"]
    targets = [tmp_path / "one.pdf", tmp_path / "two.pdf"]
    for path in staged:
        path.write_bytes(b"new")
    token = CancelToken()
    replace = Path.replace

    def cancel_after_replace(self, destination):
        result = replace(self, destination)
        token.cancel()
        return result

    monkeypatch.setattr(Path, "replace", cancel_after_replace)
    with pytest.raises(outputs.OutputCommitError) as caught:
        outputs.commit_files(zip(staged, targets), cancel=token)
    assert caught.value.committed == (targets[0],)
    assert isinstance(caught.value.cause, JobCancelled)
    assert not targets[1].exists()


def test_destination_locks_serialize_same_path(tmp_path):
    target = tmp_path / "output.pdf"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    order = []

    def first():
        with outputs.destination_lock(target):
            order.append("first")
            first_entered.set()
            release_first.wait(5)
            order.append("released")

    def second():
        first_entered.wait(5)
        with outputs.destination_lock(target.parent / "." / target.name):
            order.append("second")
            second_entered.set()

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    two.start()
    try:
        assert first_entered.wait(5)
        assert not second_entered.is_set()
    finally:
        release_first.set()
        one.join(5)
        two.join(5)
    assert order == ["first", "released", "second"]


def test_staging_plain_mkdir_is_unique_and_destination_local(tmp_path):
    first = outputs.staging_dir(tmp_path)
    second = outputs.staging_dir(tmp_path)
    assert first != second
    assert first.parent == second.parent == tmp_path
    assert first.is_dir() and second.is_dir()


def test_compression_fallback_strips_info_and_xmp_and_keeps_original(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf")
    original = source.read_bytes()
    staging_paths = []

    def bigger(gs, *args, cancel=None):
        target = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("-sOutputFile=")))
        staging_paths.append(target)
        target.write_bytes(b"x" * (len(original) + 100))

    monkeypatch.setattr(compress, "run_gs", bigger)
    first = compress.compress("unused", source)
    second = compress.compress("unused", source)
    assert first == second == tmp_path / "source_small.pdf"
    assert staging_paths[0] != staging_paths[1]
    reader = PdfReader(first)
    assert not reader.metadata
    assert "/Metadata" not in reader.trailer["/Root"]
    assert len(reader.pages) == 2
    assert source.read_bytes() == original


def test_compression_cancel_after_metadata_preserves_previous_output(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf")
    target = tmp_path / "source_small.pdf"
    target.write_bytes(b"previous")
    token = CancelToken()

    def copy_source(gs, *args, cancel=None):
        destination = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("-sOutputFile=")))
        shutil.copyfile(args[-1], destination)

    monkeypatch.setattr(compress, "run_gs", copy_source)
    with pytest.raises(JobCancelled):
        compress.compress("unused", source, cancel=token, progress=lambda event: token.cancel())
    assert target.read_bytes() == b"previous"


def test_image_export_numeric_cleanup_preserves_other_format_and_notes(tmp_path):
    source = make_pdf(tmp_path / "scan[1].pdf")
    folder = tmp_path / "scan[1]_images"
    folder.mkdir()
    stale = folder / "scan[1]_page_009.jpg"
    stale.write_bytes(b"stale")
    keep = [folder / "scan[1]_page_001.png", folder / "scan[1]_page_notes.jpg"]
    for path in keep:
        path.write_bytes(b"keep")
    result = images.to_images(source)
    assert len(result) == 2
    assert not stale.exists()
    assert all(path.read_bytes() == b"keep" for path in keep)
    for path in result:
        with Image.open(path) as image:
            assert image.format == "JPEG"
            assert not image.getexif()


def test_image_export_cancel_before_commit_keeps_old_images(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", (100,))
    target = tmp_path / "source_image.png"
    target.write_bytes(b"previous")
    token = CancelToken()
    with pytest.raises(JobCancelled):
        images.to_images(source, "png", cancel=token, progress=lambda event: token.cancel())
    assert target.read_bytes() == b"previous"


def test_image_export_uses_a4_phone_policy(tmp_path):
    source = make_pdf(tmp_path / "phone.pdf", (72 * 26,))
    # Tallness is irrelevant: this page's longest edge is a 26-inch width.
    [target] = images.to_images(source, "png", 150)
    with Image.open(target) as image:
        assert max(image.size) <= 1755
    assert images.page_scale(72 * 26, 72 * 37, 150) == pytest.approx(150 / 72 * 842 / (72 * 37))


def test_changed_source_before_split_commit_is_rejected(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", (100,))
    original = source.read_bytes()

    def mutate(event):
        source.write_bytes(original + b"\nchanged")

    with pytest.raises(outputs.OutputCommitError, match="Source changed") as caught:
        split.split_pdf(source, progress=mutate)
    assert caught.value.committed == ()
    assert not list((tmp_path / "source_pages").glob("*.pdf"))


def test_merge_keeps_cli_arbitrary_output_filename_policy(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", (100,))
    target = tmp_path / "combined.output"
    assert merge.merge_pdfs([source], target) == 1
    assert len(PdfReader(target).pages) == 1


def test_cli_adapters_do_not_reexport_algorithms():
    import pdf_compress
    import pdf_image
    import pdf_merge
    import pdf_split

    assert not hasattr(pdf_split, "split_pdf")
    assert not hasattr(pdf_split, "find_pdfs")
    assert not hasattr(pdf_merge, "merge_pdfs")
    assert not hasattr(pdf_compress, "strip_metadata")
    assert not hasattr(pdf_image, "to_images")


def test_merge_cli_existing_output_remains_usage_exit_two(tmp_path, monkeypatch):
    import pdf_merge

    first = make_pdf(tmp_path / "one.pdf", (100,))
    second = make_pdf(tmp_path / "two.pdf", (200,))
    output = tmp_path / "output.pdf"
    output.write_bytes(b"previous")
    monkeypatch.setattr("sys.argv", ["pdfMerge", str(first), str(second), "-o", str(output)])
    with pytest.raises(SystemExit) as caught:
        pdf_merge.main()
    assert caught.value.code == 2
    assert output.read_bytes() == b"previous"


def test_image_cli_invalid_dpi_remains_usage_exit_two(monkeypatch):
    import pdf_image

    monkeypatch.setattr("sys.argv", ["pdfImage", "-r", "49"])
    with pytest.raises(SystemExit) as caught:
        pdf_image.main()
    assert caught.value.code == 2


def test_destination_lock_blocks_independent_process_and_is_reentrant(tmp_path):
    target = tmp_path / "output.pdf"
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        "from powerpdf_core.outputs import destination_lock\n"
        "print('waiting', flush=True)\n"
        "with destination_lock(Path(sys.argv[1])):\n"
        "    print('acquired', flush=True)\n"
    )
    process = None
    try:
        with outputs.destination_lock(target):
            # Real nested acquisition must not deadlock against our own file lock.
            with outputs.destination_lock(target):
                process = subprocess.Popen(
                    [sys.executable, "-c", script, str(target)],
                    cwd=Path(__file__).resolve().parent,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                assert process.stdout.readline().strip() == "waiting"
                with pytest.raises(subprocess.TimeoutExpired):
                    process.communicate(timeout=0.2)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert stdout.strip() == "acquired"
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
