"""Content hashing, canonical JSON and atomic writes."""

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj) -> str:
    """Deterministic JSON: sorted keys, no whitespace, floats via repr."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def sha256_json(obj) -> str:
    return sha256_bytes(canonical_json(obj).encode("utf-8"))


def aggregate_sha256(entries) -> str:
    """Order-independent digest over (path, sha256) pairs."""
    lines = sorted(f"{e['path']}\t{e['sha256']}" for e in entries)
    return sha256_bytes("\n".join(lines).encode("utf-8"))


def atomic_write_text(path, text: str, encoding="utf-8") -> None:
    """Write via a temp file in the same directory, fsync, then os.replace.

    A crash leaves either the old file or the new one, never a torn write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path, obj, indent=2) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent, sort_keys=True, allow_nan=False) + "\n")


# ----------------------------------------------------------------- git access

def git(*args, cwd=None) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def git_blob_bytes(commit: str, path: str, cwd=None) -> bytes:
    return git("show", f"{commit}:{path}", cwd=cwd)


def git_head(cwd=None) -> str:
    return git("rev-parse", "HEAD", cwd=cwd).decode().strip()


def git_tracked(commit: str, cwd=None) -> list:
    out = git("ls-tree", "-r", "--name-only", commit, cwd=cwd).decode("utf-8")
    return [line for line in out.splitlines() if line]
