"""Synchronous, toolkit-independent jobs; adapters choose their own worker thread.

Cancellation is cooperative between native calls. Completed outputs are retained in
results even if a later input or a multi-file commit fails.
"""
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any


class JobCancelled(Exception):
    """The owning caller requested cooperative cancellation."""


class CancelToken:
    def __init__(self):
        self._event = Event()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise JobCancelled("Job cancelled")


@dataclass(frozen=True)
class Progress:
    completed: int
    total: int
    message: str = ""
    input_path: Path | None = None


ProgressSink = Callable[[Progress], None]


@dataclass(frozen=True)
class JobSpec:
    action: str
    inputs: tuple[Path, ...]
    output: Path | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "inputs", tuple(Path(path) for path in self.inputs))
        object.__setattr__(self, "output", Path(self.output) if self.output is not None else None)
        object.__setattr__(self, "options", dict(self.options))


@dataclass(frozen=True)
class JobOutcome:
    inputs: tuple[Path, ...]
    outputs: tuple[Path, ...] = ()
    pages: int | None = None
    input_bytes: int = 0
    output_bytes: int = 0
    error: str | None = None
    status: str = "success"

    @property
    def success(self) -> bool:
        return self.status == "success"


@dataclass(frozen=True)
class JobResult:
    outcomes: tuple[JobOutcome, ...]
    cancelled: bool = False

    @property
    def success(self) -> bool:
        return bool(self.outcomes) and not self.cancelled and all(item.status in {"success", "skipped"} for item in self.outcomes)

    @property
    def outputs(self) -> tuple[Path, ...]:
        return tuple(path for item in self.outcomes for path in item.outputs)

    @property
    def errors(self) -> tuple[JobOutcome, ...]:
        return tuple(item for item in self.outcomes if item.error is not None and item.status != "cancelled")

    @property
    def output_bytes(self) -> int:
        return sum(item.output_bytes for item in self.outcomes)


def _size(paths) -> int:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            pass  # Preserve the original operation/commit error in the result.
    return total


def run_job(spec: JobSpec, *, cancel: CancelToken | None = None,
            progress: ProgressSink | None = None,
            on_outcome: Callable[[JobOutcome], None] | None = None) -> JobResult:
    from .outputs import OutputCommitError
    actions = {"merge", "split", "compress", "images", "pages-save", "pages-extract"}
    if spec.action not in actions:
        raise ValueError(f"Unknown job action: {spec.action}")
    if spec.output is not None and len(spec.inputs) > 1 and spec.action not in {"merge"}:
        raise ValueError("An explicit output requires exactly one input")
    token = cancel if cancel is not None else CancelToken()
    groups = [spec.inputs] if spec.action == "merge" and spec.inputs else [(path,) for path in spec.inputs]
    outcomes = []
    cancelled = False
    for index, inputs in enumerate(groups):
        if token.is_cancelled:
            cancelled = True
            break
        output_paths, pages = (), None
        before = _size(inputs)
        try:
            token.raise_if_cancelled()
            if progress:
                progress(Progress(index, len(groups), spec.action, inputs[0]))
            options = dict(spec.options)
            if spec.action == "merge":
                from .merge import merge_pdfs
                output = spec.output or inputs[0].parent / "merged.pdf"
                pages = merge_pdfs(list(inputs), output, cancel=token, progress=progress, **options)
                output_paths = (output,)
            elif spec.action == "split":
                from .split import split_pdf
                pages, folder = split_pdf(inputs[0], output=spec.output, cancel=token, progress=progress, **options)
                width = max(3, len(str(pages)))
                output_paths = tuple(folder / f"{inputs[0].stem}_page_{i:0{width}d}.pdf" for i in range(1, pages + 1))
            elif spec.action == "compress":
                if inputs[0].stem.endswith("_small"):
                    item = JobOutcome(inputs, input_bytes=before, status="skipped")
                    outcomes.append(item)
                    if on_outcome:
                        on_outcome(item)
                    continue
                from .compress import compress
                from pdf_runtime import resolve_tool
                gs = options.pop("gs", None) or resolve_tool("ghostscript")
                if not gs:
                    raise RuntimeError("Ghostscript not found")
                output_paths = (compress(gs, inputs[0], output=spec.output, cancel=token, progress=progress, **options),)
            elif spec.action == "images":
                from .images import to_images
                output_paths = tuple(to_images(inputs[0], output=spec.output, cancel=token, progress=progress, **options))
            else:
                from .pages import PageEditSession
                session = options.pop("session", None) or PageEditSession(inputs[0])
                output = spec.output or inputs[0].with_name(f"{inputs[0].stem}_{'edited' if spec.action == 'pages-save' else 'extract'}.pdf")
                ids = list(session.order) if spec.action == "pages-save" else session.targets()
                session.write(output, ids, cancel=token, progress=progress, **options)
                pages, output_paths = len(ids), (output,)
            item = JobOutcome(inputs, output_paths, pages, before, _size(output_paths))
        except Exception as error:
            committed = tuple(error.committed) if isinstance(error, OutputCommitError) else ()
            cause = error.cause if isinstance(error, OutputCommitError) else error
            cancelled = isinstance(cause, JobCancelled)
            item = JobOutcome(inputs, committed, pages, before, _size(committed), str(error), "cancelled" if cancelled else "error")
        outcomes.append(item)
        if on_outcome:
            on_outcome(item)
        if progress:
            progress(Progress(index + 1, len(groups), item.status, inputs[0]))
        if cancelled:
            break
    return JobResult(tuple(outcomes), cancelled)
