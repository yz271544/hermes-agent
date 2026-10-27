"""Read scoped committed SQL with three Git processes, never run repository code."""
import fnmatch
import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess


def materialize(repository, revision, destination, source_id, patterns, max_bytes, max_files):
    repository = Path(repository).resolve(strict=True)
    if revision.startswith("-") or max_bytes < 1 or max_files < 1:
        raise ValueError("Invalid revision or budget")

    def git(*args):
        return subprocess.run(["git", "-C", str(repository), "--no-pager", *args],
                              capture_output=True, check=True, timeout=60).stdout

    commit = git("rev-parse", "--verify", revision + "^{commit}").decode().strip()
    entries = git("ls-tree", "-r", "-l", "-z", "--full-tree", commit).split(b"\0")
    namespace = hashlib.sha256((str(repository) + source_id + repr([patterns, max_bytes, max_files])).encode()).hexdigest()[:24]
    root = Path(destination) / namespace / commit
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError("Snapshot root must not be a symlink")
    selected, issues, blobs, hashes = [], [], {}, {}
    for entry in filter(None, entries):
        header, raw_path = entry.split(b"\t", 1)
        name = raw_path.decode("utf-8")
        path = PurePosixPath(name)
        if path.suffix.lower() not in (".sql", ".ddl") or not any(fnmatch.fnmatch(name, p) for p in patterns):
            continue
        mode, kind, oid, size = header.decode().split()
        excluded = (path.is_absolute() or ".." in path.parts or any(p.startswith(".") for p in path.parts)
                    or set(path.parts) & {"node_modules", "vendor", "target", "dist", "venv"}
                    or re.search(r"(?i)(secret|credential|private.?key)", name))
        if excluded or kind != "blob" or mode not in ("100644", "100755"):
            issues.append({"path": name, "reason": "excluded_or_nonregular_git_object"})
        elif int(size) > max_bytes:
            issues.append({"path": name, "reason": "file_budget_exceeded"})
        elif len(selected) >= max_files:
            issues.append({"path": name, "reason": "file_count_budget_exceeded"})
        else:
            selected.append((name, oid, int(size)))
    process = subprocess.Popen(["git", "-C", str(repository), "cat-file", "--batch"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for name, oid, size in selected:
            process.stdin.write((oid + "\n").encode())
            process.stdin.flush()
            response = process.stdout.readline().decode().split()
            if response != [oid, "blob", str(size)]:
                raise ValueError("Unexpected Git object response")
            raw = process.stdout.read(size)
            if len(raw) != size or process.stdout.read(1) != b"\n":
                raise ValueError("Incomplete Git object")
            target = root / name
            if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
                raise ValueError("Unsafe snapshot path")
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.read_bytes() != raw:
                target.write_bytes(raw)
            blobs[name] = oid
            hashes[name] = hashlib.sha256(raw).hexdigest()
    finally:
        process.stdin.close()
        process.stdout.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if process.returncode:
        raise ValueError("Git batch reader failed")
    if not selected:
        issues.append({"reason": "no_scoped_sql_files"})
    return root, {"read_mode": "git_commit", "repository": str(repository), "commit": commit,
                  "patterns": patterns, "issues": issues, "files": blobs, "file_hashes": hashes}
