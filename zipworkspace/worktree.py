"""One client-owned SQLite catalog per checkout, outside both its tree and share."""
from __future__ import annotations
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from .archive import read_archive
from .freeze import frozen_tree, pack
from .model import Commit, Rejected, check_id, new_id
from .publish import confirm_final, digest_file, publish, rename_new, sync_dir


def paths(work, store, state):
    work, store, state = (Path(p).absolute() for p in (work, store, state))
    normalized = [p.resolve() for p in (work, store, state)]
    if any(a.is_relative_to(b) or b.is_relative_to(a)
           for i, a in enumerate(normalized) for b in normalized[i + 1:]):
        raise Rejected("worktree, archive store and private state must be independent directories")
    if not store.is_dir() or store.is_symlink(): raise Rejected("select an existing archive-store directory")
    return work, store, state


def enroll(work, store, state, base=None):
    work, store, state = paths(work, store, state)
    if not work.is_dir() or work.is_symlink(): raise Rejected("select a regular local working directory")
    state.mkdir(mode=0o700, exist_ok=False)
    con = sqlite3.connect(state / "worktree.sqlite")
    try:
        con.execute("PRAGMA synchronous=FULL")
        con.execute("""CREATE TABLE checkout(singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          worktree TEXT NOT NULL, location TEXT NOT NULL, store TEXT NOT NULL, repo TEXT NOT NULL,
          branch TEXT NOT NULL, generation INTEGER NOT NULL, parent TEXT, base_sha TEXT,
          pending BLOB, pending_sha TEXT)""")
        con.execute("CREATE TABLE completed(operation TEXT PRIMARY KEY, marker BLOB NOT NULL, digest TEXT NOT NULL)")
        root, source_sha = base if base else (None, None)
        con.execute("INSERT INTO checkout VALUES(1,?,?,?,?,?,?,?,?,NULL,NULL)", (
            new_id(), str(work), str(store), root.repo if root else new_id(),
            root.branch if root else new_id(), root.generation if root else 0,
            root.operation if root else None, source_sha))
        con.commit()
    finally: con.close()
    sync_dir(state); sync_dir(state.parent)


@contextmanager
def locked(state):
    state = Path(state).absolute()
    if not state.is_dir() or state.is_symlink() or (state.stat().st_mode & 0o077):
        raise Rejected("use the existing private worktree state directory")
    # mode=rw will not create a blank authority if an original catalog goes missing.
    con = sqlite3.connect((state / "worktree.sqlite").as_uri() + "?mode=rw", uri=True, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA synchronous=FULL")
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM checkout WHERE singleton=1").fetchone()
        if row is None: raise Rejected("worktree record missing")
        yield con, row, state
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally: con.close()


def extract(archive, dest, managed=True):
    dest = Path(dest).absolute()
    if dest.exists() or dest.is_symlink(): raise Rejected("checkout needs a fresh destination")
    # A request that cannot complete does not leave a partially usable checkout.
    stage = Path(tempfile.mkdtemp(prefix=".zip-checkout-", dir=dest.parent))
    try:
        marker, manifest = read_archive(archive, managed=managed, sink=stage)
        rename_new(stage, dest)
        sync_dir(dest.parent)
        return marker, manifest
    finally:
        if stage.exists():
            # Only private locally-created output, never the user's prior destination.
            import shutil
            shutil.rmtree(stage)


def checkout(archive, dest, store, state):
    dest, store, state = paths(dest, store, state)
    if state.exists(): raise Rejected("choose a new private state directory")
    before = digest_file(Path(archive))
    marker, _ = extract(archive, dest)
    if digest_file(Path(archive)) != before:
        raise Rejected("selected source archive changed; the copy was not attached")
    enroll(dest, store, state, (marker, before))
    return marker


def finish(state, operation):
    operation = check_id(operation)
    with locked(state) as (con, row, state):
        done = con.execute("SELECT marker,digest FROM completed WHERE operation=?", (operation,)).fetchone()
        if done is not None:
            marker = Commit.decode(done["marker"])
            target = Path(row["store"]) / marker.filename
            if marker.operation != operation or not confirm_final(target, marker, done["digest"]):
                raise Rejected("completed original archive is unavailable; no replacement was published")
            return target, marker
        if row["pending"] is None: raise Rejected("no original operation recorded here; not reconstructing it")
        marker = Commit.decode(row["pending"])
        if marker.operation != operation: raise Rejected("another operation is pending; no worktree was recaptured")
        local = state / (marker.operation + ".zxs")
        target = publish(local, Path(row["store"]), marker, row["pending_sha"])
        con.execute("INSERT INTO completed(operation,marker,digest) VALUES(?,?,?)", (operation, marker.encode(), row["pending_sha"]))
        con.execute("""UPDATE checkout SET branch=?,generation=?,parent=?,base_sha=?,pending=NULL,pending_sha=NULL
                       WHERE singleton=1""", (marker.branch, marker.generation, marker.operation, row["pending_sha"]))
        # The immutable private spool remains recoverable; cleanup is not part of this prototype.
    return target, marker


def commit(state, *, fork=False):
    with locked(state) as (con, row, state):
        if row["pending"] is not None:
            raise Rejected("an original operation is pending; resume it rather than recapturing")
        work, store, state = paths(Path(row["location"]), Path(row["store"]), state)
        gen, branch, repo = row["generation"], row["branch"], row["repo"]
        if gen:
            base_path = store / f"{repo}.{branch}.{gen:016x}.zxs"
            saved, _ = read_archive(base_path)
            if (saved.repo, saved.branch, saved.generation, saved.operation) != (repo, branch, gen, row["parent"]) or digest_file(base_path) != row["base_sha"]:
                raise Rejected("exact original branch base is not available in the selected store")
        operation = new_id()
        with frozen_tree(work, state) as (frozen, manifest):
            marker = Commit(repo, new_id() if fork else branch, 1 if fork else gen + 1,
                            operation, row["parent"], manifest.digest())
            local = state / (operation + ".zxs")
            pack(frozen, manifest, marker, local)
        if read_archive(local)[0] != marker: raise Rejected("local closed archive verification failed")
        digest = digest_file(local)
        sync_dir(state)
        con.execute("UPDATE checkout SET pending=?,pending_sha=? WHERE singleton=1", (marker.encode(), digest))
    # Durable client intent precedes any remote bytes. A retry uses this exact
    # candidate, regardless of later writes to the local working directory.
    return finish(state, marker.operation)


def status(state):
    with locked(state) as (_, row, _):
        return {"worktree": row["worktree"], "repo": row["repo"], "branch": row["branch"],
                "base_generation": row["generation"], "base_operation": row["parent"],
                "pending_operation": Commit.decode(row["pending"]).operation if row["pending"] is not None else None}


def history(archive, store):
    """Follow original parent operations only. Missing history is not a synthetic root."""
    first, _ = read_archive(archive)
    index = {first.operation: first}
    store = Path(store)
    seen = 0
    for item in store.iterdir():
        seen += 1
        if seen > 10_000: raise Rejected("history listing exceeds current catalog budget")
        if not item.name.startswith(first.repo + ".") or item.suffix != ".zxs": continue
        record, _ = read_archive(item)
        if record.repo != first.repo or item.name != record.filename:
            raise Rejected("managed slot conflicts with its own directory commit")
        if record.operation in index and index[record.operation] != record:
            raise Rejected("one operation resolves to distinct directory commits")
        index[record.operation] = record
    chain, used = [], set()
    cursor = first
    while True:
        if cursor.operation in used: raise Rejected("directory history cycle")
        used.add(cursor.operation); chain.append(cursor)
        if cursor.parent is None: return chain, None
        if cursor.parent not in index: return chain, cursor.parent
        prev = index[cursor.parent]
        if cursor.generation > 1 and (prev.branch != cursor.branch or prev.generation + 1 != cursor.generation):
            raise Rejected("broken branch ancestry")
        cursor = prev
