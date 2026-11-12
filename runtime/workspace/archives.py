"""Archive closed batches and restore their original paths on demand."""
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

from runtime.workspace.storage import path_reference
from runtime.console import progress


def _remove_batch(directory):
    """Move the extracted directory aside before deletion so interrupted cleanup is recoverable."""
    if (directory.is_symlink() or directory.is_junction() or directory.resolve() != directory
            or not directory.with_suffix(".zip").is_file()):
        raise ValueError(f"Cannot safely remove archived batch directory: {directory}")
    with TemporaryDirectory(prefix=f".{directory.name}-", dir=directory.parent) as temporary:
        directory.rename(Path(temporary) / directory.name)


def restore_batch(directory):
    """Publish a fully extracted batch atomically; leave existing directories alone."""
    directory = Path(directory).resolve()
    archive = directory.with_suffix(".zip")
    if directory.exists() or not archive.is_file():
        return False
    with TemporaryDirectory(prefix=f".{directory.name}-", dir=directory.parent) as temporary:
        staging = Path(temporary)
        target = staging / directory.name
        with ZipFile(archive) as stored:
            for member in stored.infolist():
                if not (staging / member.filename).resolve().is_relative_to(target):
                    raise ValueError(f"Archive member exceeds batch directory: {member.filename}")
            stored.extractall(staging)
        if not target.is_dir():
            raise ValueError(f"Archive does not contain batch directory: {directory.name}")
        target.rename(directory)
    return True


def archive_batch(directory):
    """Verify a complete ZIP before removing the original batch directory."""
    directory = Path(directory).absolute()
    if directory.is_symlink() or directory.is_junction() or directory.resolve() != directory:
        raise ValueError(f"Refusing to archive a linked batch directory: {directory}")
    archive = directory.with_suffix(".zip")
    if archive.exists():
        raise FileExistsError(f"Batch archive already exists: {archive}")
    temporary = archive.with_suffix(".zip.tmp")
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED, compresslevel=6) as stored:
            for path in sorted(directory.rglob("*")):
                if path.is_symlink() or path.is_junction():
                    raise ValueError(f"Refusing to archive a linked batch entry: {path}")
                stored.write(path, path.relative_to(directory.parent).as_posix())
        with ZipFile(temporary) as stored:
            corrupt = stored.testzip()
            if corrupt is not None:
                raise ValueError(f"Batch archive failed verification: {corrupt}")
        temporary.replace(archive)
    finally:
        temporary.unlink(missing_ok=True)
    _remove_batch(directory)


class BatchArchives:
    """Track only directories restored by this experiment for round-end cleanup."""

    def __init__(self):
        self.restored = set()

    def restore(self, directory):
        directory = Path(directory).resolve()
        if restore_batch(directory):
            self.restored.add(directory)

    def finish(self, batches=None):
        """Archive new batches and discard unchanged extracted copies without recompression."""
        errors = []
        directories = []
        try:
            if batches is not None and Path(batches).is_dir():
                directories = sorted(Path(batches).iterdir())
        except OSError as exc:
            errors.append(dict(batch=path_reference(batches), error=f"{type(exc).__name__}: {exc}"))
        for directory in directories + sorted(self.restored):
            try:
                if not directory.is_dir():
                    continue
                if directory in self.restored:
                    _remove_batch(directory)
                else:
                    progress("archive", f"Compressing {path_reference(directory)}")
                    archive_batch(directory)
            except Exception as exc:
                errors.append(dict(batch=path_reference(directory), error=f"{type(exc).__name__}: {exc}"))
        self.restored.clear()
        return errors
