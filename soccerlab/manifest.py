"""Immutable manifests: freeze and verify.

A manifest pins a model version to exact bytes: code files, data files,
hyperparameters, learned coefficients and golden predictions, all tied to a
git commit. Verification fails closed when anything protected changes.

    python -m soccerlab manifest verify registry/soccer-dc-legacy/manifest.json

Checks, in order:

1. integrity   manifest_sha256 matches the manifest body (no hand edits)
2. history     every recorded file hashes identically at the recorded commit
3. worktree    protected files in the checkout still match (unprotected files,
               e.g. schedules the production task refreshes, only warn)
4. golden      (optional) the model, re-run on the commit's data, reproduces
               its stored coefficients and golden predictions
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .hashing import git_blob_bytes, sha256_bytes, sha256_file, sha256_json, aggregate_sha256
from .paths import REPO_ROOT

SCHEMA = "soccerlab.manifest/1"


@dataclass
class VerifyReport:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    checked: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines = [f"  {k}: {v}" for k, v in self.checked.items()]
        lines += [f"  WARN  {w}" for w in self.warnings]
        lines += [f"  FAIL  {e}" for e in self.errors]
        lines.append("VERIFY " + ("PASSED" if self.ok else "FAILED"))
        return "\n".join(lines)


def seal(manifest: dict) -> dict:
    """Attach manifest_sha256 over everything except the seal itself."""
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    out = dict(body)
    out["manifest_sha256"] = sha256_json(body)
    return out


def load(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def freeze(manifest: dict, path, overwrite=False) -> dict:
    """Write a sealed manifest. Refuses to overwrite an existing version."""
    from .hashing import atomic_write_json
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} exists; model versions are immutable -- create a new version id")
    sealed = seal(manifest)
    atomic_write_json(path, sealed)
    return sealed


def file_entry(path: str, data: bytes, protected=True, note=None) -> dict:
    e = {"path": path, "sha256": sha256_bytes(data), "bytes": len(data), "protected": protected}
    if note:
        e["note"] = note
    return e


def _git_equivalent(commit, paths, repo_root) -> dict:
    """path -> True if `git hash-object` of the working file (with git's
    clean filters, e.g. CRLF->LF) equals the blob id at `commit`."""
    import subprocess
    if not paths:
        return {}
    try:
        tree = subprocess.run(["git", "ls-tree", "-r", commit], cwd=repo_root, check=True,
                              capture_output=True).stdout.decode("utf-8")
        blob = {}
        for line in tree.splitlines():
            meta, path = line.split("\t", 1)
            blob[path] = meta.split()[2]
        out = subprocess.run(["git", "hash-object", "--stdin-paths"], cwd=repo_root, check=True,
                             input="\n".join(paths).encode("utf-8"), capture_output=True).stdout
        ids = out.decode().split()
        return {p: blob.get(p) == i for p, i in zip(paths, ids)}
    except Exception:                                            # noqa: BLE001
        return {}


def verify(manifest_path,repo_root=None, check_history=True, check_worktree=True,
           golden_fn=None) -> VerifyReport:
    repo_root = Path(repo_root or REPO_ROOT)
    rep = VerifyReport()
    m = load(manifest_path)

    if m.get("schema") != SCHEMA:
        rep.errors.append(f"unknown schema {m.get('schema')!r}")
        return rep

    body = {k: v for k, v in m.items() if k != "manifest_sha256"}
    if sha256_json(body) != m.get("manifest_sha256"):
        rep.errors.append("manifest_sha256 mismatch: manifest body was edited after freezing")
    rep.checked["integrity"] = "ok" if not rep.errors else "FAILED"

    files = list(m.get("code_files", [])) + list(m.get("data_files", []))
    for group in ("code_files", "data_files"):
        agg_key = f"{group}_aggregate_sha256"
        if agg_key in m and aggregate_sha256(m[group]) != m[agg_key]:
            rep.errors.append(f"{agg_key} does not match listed entries")

    commit = m.get("git", {}).get("commit")
    if check_history:
        bad = 0
        for e in files:
            try:
                blob = git_blob_bytes(commit, e["path"], cwd=repo_root)
            except Exception:                                    # noqa: BLE001
                rep.errors.append(f"history: {e['path']} missing at {commit[:10]}")
                bad += 1
                continue
            if sha256_bytes(blob) != e["sha256"]:
                rep.errors.append(f"history: {e['path']} differs at {commit[:10]}")
                bad += 1
        rep.checked["history"] = f"{len(files) - bad}/{len(files)} files match commit {commit[:10]}"

    if check_worktree:
        drift_p = drift_u = 0
        present = [e for e in files if (repo_root / e["path"]).exists()]
        eol_ok = _git_equivalent(commit, [e["path"] for e in present], repo_root)
        for e in files:
            p = repo_root / e["path"]
            if not p.exists():
                msg = f"worktree: {e['path']} missing"
                (rep.errors if e.get("protected", True) else rep.warnings).append(msg)
                drift_p += e.get("protected", True)
                drift_u += not e.get("protected", True)
                continue
            # Git may check text files out with CRLF line endings; a file is
            # unchanged if either its raw bytes or its git-cleaned form match.
            if sha256_file(p) != e["sha256"] and not eol_ok.get(e["path"], False):
                if e.get("protected", True):
                    rep.errors.append(f"worktree: protected file changed: {e['path']}")
                    drift_p += 1
                else:
                    rep.warnings.append(f"worktree: unprotected file drifted (expected for live data): {e['path']}")
                    drift_u += 1
        rep.checked["worktree"] = f"{drift_p} protected changes, {drift_u} unprotected drifts"

    if golden_fn is not None:
        try:
            errs = golden_fn(m)
        except Exception as exc:                                 # noqa: BLE001
            errs = [f"golden recompute raised {type(exc).__name__}: {exc}"]
        rep.errors.extend(errs)
        rep.checked["golden"] = "ok" if not errs else f"{len(errs)} mismatches"
    return rep
