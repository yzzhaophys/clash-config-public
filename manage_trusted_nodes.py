#!/usr/bin/env python3
"""Safely add or replace entries in the private trusted-nodes.yaml file."""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

import generate_raw_nodes as generator


class TrustedNodesError(ValueError):
    """A user-facing trusted-nodes validation or merge error."""


@dataclass(frozen=True)
class MergeResult:
    added: int
    updated: int
    unchanged: int
    preserved: int
    changed: bool
    backup: Path | None


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise TrustedNodesError(f"{label} 不能是符号链接：{path}")


def _load_document(path: Path, *, required: bool) -> dict[str, Any]:
    path = path.expanduser()
    _reject_symlink(path, "trusted-nodes 文件")
    if not path.exists():
        if required:
            raise TrustedNodesError(f"trusted-nodes 文件不存在：{path}")
        return {"nodes": []}
    if not path.is_file():
        raise TrustedNodesError(f"trusted-nodes 路径不是普通文件：{path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise TrustedNodesError(f"{path}: 不是有效 YAML：{exc}") from exc

    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise TrustedNodesError(f"{path}: 顶层必须是映射，并包含 nodes 列表")
    if "proxies" in data:
        raise TrustedNodesError(
            f"{path}: trusted-nodes.yaml 只支持 nodes 列表，不能包含顶层 proxies"
        )
    if "nodes" not in data:
        if data:
            raise TrustedNodesError(f"{path}: 顶层必须包含 nodes 列表")
        data["nodes"] = []
    if data["nodes"] is None:
        data["nodes"] = []
    if not isinstance(data["nodes"], list):
        raise TrustedNodesError(f"{path}: nodes 必须是列表")
    return data


def _node_id(node: Any, *, path: Path, index: int) -> str:
    field = f"{path}: nodes[{index}]"
    if not isinstance(node, dict):
        raise TrustedNodesError(f"{field} 必须是映射")
    value = node.get("id")
    if not isinstance(value, str) or not value.strip():
        raise TrustedNodesError(f"{field}.id 必须是非空字符串")
    value = value.strip()
    if "\n" in value or "\r" in value:
        raise TrustedNodesError(f"{field}.id 不能包含换行")
    return value


def _node_key(node: Any, *, path: Path, index: int) -> tuple[str, str]:
    node_id = _node_id(node, path=path, index=index)
    proxy = node.get("proxy")
    protocol = ""
    if isinstance(proxy, dict):
        protocol = str(proxy.get("type", "")).strip().lower()
    return node_id, protocol


def _index_nodes(nodes: list[Any], *, path: Path) -> dict[tuple[str, str], int]:
    indexes: dict[tuple[str, str], int] = {}
    for index, node in enumerate(nodes):
        key = _node_key(node, path=path, index=index)
        if key in indexes:
            node_id, protocol = key
            raise TrustedNodesError(
                f"{path}: id={node_id!r}、protocol={protocol or '<empty>'!r} 重复；"
                "同一物理节点的不同协议可以分别登记"
            )
        indexes[key] = index
    return indexes


def _validate_nodes(nodes: list[Any], *, path: Path) -> None:
    _index_nodes(nodes, path=path)
    try:
        generator.normalize_trusted_nodes(
            copy.deepcopy(nodes),
            {},
            path,
        )
    except (TypeError, ValueError) as exc:
        raise TrustedNodesError(str(exc)) from exc


def _merge_documents(
    existing: dict[str, Any], incoming: dict[str, Any], *, target: Path, source: Path
) -> tuple[dict[str, Any], int, int, int, int]:
    existing_nodes = existing.get("nodes", [])
    incoming_nodes = incoming.get("nodes", [])
    existing_indexes = _index_nodes(existing_nodes, path=target)
    _index_nodes(incoming_nodes, path=source)

    merged = copy.deepcopy(existing)
    merged_nodes = copy.deepcopy(existing_nodes)
    added = updated = unchanged = 0
    for index, incoming_node in enumerate(incoming_nodes):
        key = _node_key(incoming_node, path=source, index=index)
        existing_index = existing_indexes.get(key)
        if existing_index is None:
            existing_indexes[key] = len(merged_nodes)
            merged_nodes.append(copy.deepcopy(incoming_node))
            added += 1
        elif merged_nodes[existing_index] == incoming_node:
            unchanged += 1
        else:
            merged_nodes[existing_index] = copy.deepcopy(incoming_node)
            updated += 1
    merged["nodes"] = merged_nodes
    _validate_nodes(merged_nodes, path=target)
    return merged, added, updated, unchanged, len(existing_nodes)


@contextmanager
def _exclusive_lock(target: Path) -> Iterator[None]:
    lock_path = target.with_name(f".{target.name}.lock")
    _reject_symlink(lock_path, "trusted-nodes 锁文件")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise TrustedNodesError(
                f"trusted-nodes 锁文件不能是符号链接：{lock_path}"
            ) from exc
        raise
    handle = None
    try:
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "a+", encoding="utf-8")
        fd = None
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if handle is not None:
            handle.close()
        elif fd is not None:
            os.close(fd)


def _backup_path(target: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = target.with_name(f"{target.name}.bak-{stamp}")
    for index in range(1000):
        candidate = base if index == 0 else target.with_name(f"{base.name}-{index}")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with target.open("rb") as source, os.fdopen(fd, "wb") as backup:
                shutil.copyfileobj(source, backup)
                backup.flush()
                os.fsync(backup.fileno())
            os.chmod(candidate, 0o600)
            return candidate
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
    raise TrustedNodesError(f"无法为 {target} 创建不碰撞的备份路径")


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_yaml(target: Path, document: dict[str, Any]) -> None:
    fd, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(
                document,
                handle,
                allow_unicode=True,
                sort_keys=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _merge_once(target: Path, source: Path, *, apply: bool) -> MergeResult:
    existing = _load_document(target, required=False)
    incoming = _load_document(source, required=True)
    merged, added, updated, unchanged, preserved = _merge_documents(
        existing,
        incoming,
        target=target,
        source=source,
    )
    changed = added > 0 or updated > 0
    backup = None
    if apply and changed:
        target.parent.mkdir(parents=True, exist_ok=True)
        _reject_symlink(target, "目标 trusted-nodes 文件")
        if target.exists():
            backup = _backup_path(target)
        _atomic_write_yaml(target, merged)
    return MergeResult(added, updated, unchanged, preserved, changed, backup)


def merge_trusted_nodes_file(
    target: Path, source: Path, *, apply: bool = False
) -> MergeResult:
    """Preview or apply an ID-based merge from ``source`` into ``target``."""
    target = target.expanduser()
    source = source.expanduser()
    _reject_symlink(source, "源 trusted-nodes 文件")
    if target.resolve() == source.resolve():
        raise TrustedNodesError("源文件和目标文件不能是同一个文件")
    if apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(target):
            return _merge_once(target, source, apply=True)
    return _merge_once(target, source, apply=False)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按 id + proxy.type 合并私有 trusted-nodes.yaml，不覆盖其他节点"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    merge = subparsers.add_parser(
        "merge",
        help="预览或应用节点追加/按 id 更新",
    )
    merge.add_argument("--target", required=True, type=Path, help="目标 trusted-nodes.yaml")
    merge.add_argument(
        "--source",
        required=True,
        type=Path,
        help="包含 nodes 列表的新节点私密 YAML；不接受 proxies 格式",
    )
    merge.add_argument(
        "--apply",
        action="store_true",
        help="实际写入；省略时只预览，不创建备份或修改目标",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = merge_trusted_nodes_file(
            args.target,
            args.source,
            apply=args.apply,
        )
    except (OSError, TrustedNodesError) as exc:
        print(f"trusted-nodes 合并失败：{exc}", file=os.sys.stderr)
        return 1

    mode = "已应用" if args.apply else "预览"
    print(
        f"trusted-nodes {mode}：新增={result.added}，更新={result.updated}，"
        f"不变={result.unchanged}，保留={result.preserved}，"
        f"changed={'yes' if result.changed else 'no'}"
    )
    if result.backup is not None:
        print("已创建目标文件备份；未输出节点内容。")
    elif not args.apply and result.changed:
        print("未写入目标；确认后追加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
