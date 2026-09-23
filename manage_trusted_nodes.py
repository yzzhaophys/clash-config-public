#!/usr/bin/env python3
"""View, merge, remove and restore private trusted inventory with safe writes."""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

import generate_raw_nodes as generator
from node_io import load_yaml


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


@dataclass(frozen=True)
class RemoveResult:
    removed: int
    preserved: int
    changed: bool
    backup: Path | None


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise TrustedNodesError(f"{label} 不能是符号链接：{path}")


def _require_private_permissions(path: Path, label: str) -> None:
    mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    if mode & 0o077:
        raise TrustedNodesError(
            f"{label} 权限过宽：{path} 当前为 {mode:04o}；请先执行 chmod 600"
        )


def _load_document(
    path: Path, *, required: bool, require_private: bool = True
) -> dict[str, Any]:
    path = path.expanduser()
    _reject_symlink(path, "trusted-nodes 文件")
    if not path.exists():
        if required:
            raise TrustedNodesError(f"trusted-nodes 文件不存在：{path}")
        return {"nodes": []}
    if not path.is_file():
        raise TrustedNodesError(f"trusted-nodes 路径不是普通文件：{path}")
    if require_private:
        _require_private_permissions(path, "trusted-nodes 文件")
    try:
        data = load_yaml(path)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
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


def _same_yaml_value(left: Any, right: Any) -> bool:
    """Compare YAML values without equating booleans, integers and floats."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return len(left) == len(right) and all(
            any(_same_yaml_value(key, other_key) and _same_yaml_value(value, other_value)
                for other_key, other_value in right.items())
            for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_yaml_value(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, set):
        return len(left) == len(right) and all(
            any(_same_yaml_value(a, b) for b in right) for a in left
        )
    return left == right


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
        elif _same_yaml_value(merged_nodes[existing_index], incoming_node):
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


def _normalize_remove_selector(
    node_id: str, protocol: str | None
) -> tuple[str, str | None]:
    normalized_id = str(node_id).strip()
    if not normalized_id or "\n" in normalized_id or "\r" in normalized_id:
        raise TrustedNodesError("删除目标 id 必须是无换行的非空字符串")
    normalized_protocol = (
        str(protocol).strip().lower() if protocol is not None else None
    )
    if normalized_protocol not in {None, "vless", "hysteria2", "socks5"}:
        raise TrustedNodesError("删除目标 protocol 必须是 vless、hysteria2 或 socks5")
    return normalized_id, normalized_protocol


def _remove_once(
    target: Path,
    node_id: str,
    protocol: str | None,
    *,
    apply: bool,
) -> RemoveResult:
    existing = _load_document(target, required=True)
    existing_nodes = existing.get("nodes", [])
    _validate_nodes(existing_nodes, path=target)

    kept_nodes: list[Any] = []
    removed = 0
    for index, node in enumerate(existing_nodes):
        current_id, current_protocol = _node_key(node, path=target, index=index)
        if current_id == node_id and (
            protocol is None or current_protocol == protocol
        ):
            removed += 1
        else:
            kept_nodes.append(copy.deepcopy(node))
    if removed == 0:
        selector = f"id={node_id!r}"
        if protocol is not None:
            selector += f"、protocol={protocol!r}"
        raise TrustedNodesError(f"目标文件中未找到 {selector}")

    updated = copy.deepcopy(existing)
    updated["nodes"] = kept_nodes
    _validate_nodes(kept_nodes, path=target)
    backup = None
    if apply:
        _reject_symlink(target, "目标 trusted-nodes 文件")
        backup = _backup_path(target)
        _atomic_write_yaml(target, updated)
    return RemoveResult(removed, len(kept_nodes), True, backup)


def merge_trusted_nodes_file(
    target: Path, source: Path, *, apply: bool = False
) -> MergeResult:
    """Preview or apply an ID-based merge from ``source`` into ``target``."""
    target = target.expanduser()
    source = source.expanduser()
    _reject_symlink(source, "源 trusted-nodes 文件")
    if target.resolve() == source.resolve() or (
        target.exists() and source.exists() and target.samefile(source)
    ):
        raise TrustedNodesError("源文件和目标文件不能是同一个文件")
    if apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(target):
            return _merge_once(target, source, apply=True)
    return _merge_once(target, source, apply=False)


def remove_trusted_nodes_file(
    target: Path,
    node_id: str,
    *,
    protocol: str | None = None,
    apply: bool = False,
) -> RemoveResult:
    """Preview or remove one protocol or every protocol for a stable node ID."""
    target = target.expanduser()
    normalized_id, normalized_protocol = _normalize_remove_selector(node_id, protocol)
    if apply:
        with _exclusive_lock(target):
            return _remove_once(
                target,
                normalized_id,
                normalized_protocol,
                apply=True,
            )
    return _remove_once(target, normalized_id, normalized_protocol, apply=False)


def list_nodes(target: Path) -> list[dict[str, Any]]:
    """Display only inventory metadata, never connection parameters."""
    nodes = _load_document(target, required=False)["nodes"]
    _validate_nodes(nodes, path=target)
    normalized = generator.normalize_trusted_nodes(copy.deepcopy(nodes), {}, target)
    for index, (node, proxy) in enumerate(zip(nodes, normalized), 1):
        node_id, protocol = _node_key(node, path=target, index=index - 1)
        capabilities = [label for key, label in (
            ('_allow-relay', 'Relay'), ('_allow-chain-exit', 'Chain'),
            ('_allow-direct-exit', 'Direct'), ('_allow-showip', 'ShowIP'),
            ('_allow-download', 'Download'),
        ) if proxy[key]]
        if proxy['_exit-type'] == 'homeip':
            capabilities.append('HomeIP')
        print(f"{index}. {node_id!r} | {generator.normalize_region(node['region']).upper()}"
              f" | {protocol} | {', '.join(capabilities) or '无出口能力'}")
    if not nodes:
        print("当前没有节点。")
    return nodes


def preview_changes(existing: dict, updated: dict, target: Path) -> None:
    """Report identities and changed fields, never field values or nested keys."""
    before = {_node_key(n, path=target, index=i): n for i, n in enumerate(existing['nodes'])}
    after = {_node_key(n, path=target, index=i): n for i, n in enumerate(updated['nodes'])}
    for key in dict.fromkeys([*before, *after]):
        label = f"ID={key[0]!r}，协议={key[1]}"
        if key not in before:
            print(f"  新增：{label}")
        elif key not in after:
            print(f"  删除：{label}")
        elif not _same_yaml_value(before[key], after[key]):
            fields = []
            for field in dict.fromkeys([*before[key], *after[key]]):
                if field not in before[key] or field not in after[key] or not _same_yaml_value(before[key][field], after[key][field]):
                    if field == 'proxy':
                        left, right = before[key]['proxy'], after[key]['proxy']
                        fields.extend(f"proxy.{name}" for name in dict.fromkeys([*left, *right])
                                      if name not in left or name not in right or not _same_yaml_value(left[name], right[name]))
                    else:
                        fields.append(str(field))
            print(f"  更新：{label}；字段=" + ', '.join(repr(field) for field in fields))
    metadata_before = {k: v for k, v in existing.items() if k != 'nodes'}
    metadata_after = {k: v for k, v in updated.items() if k != 'nodes'}
    if not _same_yaml_value(metadata_before, metadata_after):
        print("  inventory 顶层附加字段发生变化（不显示内容）。")


def choose_file(directory: Path, target: Path, *, backups: bool = False) -> Path | None:
    if directory.exists() and not directory.is_dir():
        raise TrustedNodesError(f"文件目录无效：{directory}")
    candidates = []
    for path in directory.iterdir() if directory.exists() else ():
        matches = (path.name.startswith(target.name + '.bak-') if backups
                   else path.suffix.lower() in {'.yaml', '.yml'})
        if not matches or path.is_symlink() or not path.is_file():
            continue
        if path.resolve() == target.resolve() or (target.exists() and path.samefile(target)):
            continue
        candidates.append(path)
    candidates.sort(key=lambda p: p.name, reverse=backups)
    print(f"{'备份' if backups else '导入'}目录：{directory}")
    for index, path in enumerate(candidates, 1):
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        print(f"{index}. {path.name!r}  修改时间：{stamp}")
    if backups and not candidates:
        print("没有可用备份。")
        return None
    prompt = ("选择备份编号（0/回车取消）：" if backups else
              "选择文件编号，或输入源 YAML 路径（0/回车取消）：")
    value = input(prompt).strip()
    if value in {'', '0'}:
        return None
    if value.isascii() and value.isdigit():
        index = int(value) - 1
        if 0 <= index < len(candidates):
            return candidates[index]
        raise TrustedNodesError("文件编号无效")
    if backups:
        raise TrustedNodesError("备份编号无效")
    return Path(value).expanduser()


def interactive_restore(target: Path) -> None:
    source = choose_file(target.parent, target, backups=True)
    if source is None:
        return
    restored = _load_document(source, required=True)
    _validate_nodes(restored['nodes'], path=source)
    with _exclusive_lock(target):
        existing = _load_document(target, required=False)
        _validate_nodes(existing['nodes'], path=target)
        if source.resolve() == target.resolve() or (target.exists() and source.samefile(target)):
            raise TrustedNodesError("备份和目标不能是同一个文件")
        print(f"恢复预览：将用备份完整替换 inventory，当前={len(existing['nodes'])}，"
              f"恢复后={len(restored['nodes'])}")
        preview_changes(existing, restored, target)
        if _same_yaml_value(existing, restored):
            print("无需修改。")
            return
        if input("确认恢复？[y/N]：").strip().lower() not in {'y', 'yes'}:
            print("已取消。")
            return
        # The loaded document is the exact snapshot reviewed above.
        backup = _backup_path(target) if target.exists() else None
        _atomic_write_yaml(target, restored)
        print("已恢复。" + ("恢复前的 inventory 已备份。" if backup else ""))


def interactive_menu(target: Path, import_dir: Path | None = None) -> int:
    import_dir = import_dir or target.parent / 'imports'
    print(f"当前 inventory：{target}")
    try:
        while True:
            print("\n1. 查看节点\n2. 按编号删除节点\n3. 从 YAML 批量导入\n4. 恢复备份\n0. 退出")
            choice = input("请选择：").strip()
            if choice == '0':
                return 0
            try:
                if choice == '1':
                    list_nodes(target)
                elif choice == '2':
                    if not target.exists():
                        list_nodes(target)
                        continue
                    # Keep the displayed selection and its application under one lock.
                    with _exclusive_lock(target):
                        nodes = list_nodes(target)
                        if not nodes:
                            continue
                        selected = input("删除第几个节点？（0/回车取消）：").strip()
                        if selected in {'', '0'}:
                            continue
                        if not selected.isascii() or not selected.isdigit() or not 1 <= int(selected) <= len(nodes):
                            print("编号无效。")
                            continue
                        index = int(selected) - 1
                        node_id, protocol = _node_key(nodes[index], path=target, index=index)
                        siblings = [node for i, node in enumerate(nodes)
                                    if _node_key(node, path=target, index=i)[0] == node_id]
                        if len(siblings) > 1:
                            scope = input("1. 仅所选协议  2. 同 ID 全部协议（回车取消）：").strip()
                            if scope not in {'1', '2'}:
                                continue
                            if scope == '2':
                                protocol = None
                        result = _remove_once(target, node_id, protocol, apply=False)
                        print(f"预览：删除 ID={node_id!r}，协议={protocol or '全部'}，"
                              f"删除={result.removed}，保留={result.preserved}")
                        if input("确认删除？[y/N]：").strip().lower() not in {'y', 'yes'}:
                            print("已取消。")
                            continue
                        _remove_once(target, node_id, protocol, apply=True)
                        print("已删除并创建私有备份。")
                elif choice == '3':
                    source = choose_file(import_dir, target)
                    if source is None:
                        continue
                    merge_trusted_nodes_file(target, source)
                    # Freeze the reviewed source in a private temporary directory.
                    incoming = _load_document(source, required=True)
                    with tempfile.TemporaryDirectory(prefix='trusted-import-') as directory:
                        snapshot = Path(directory) / 'source.yaml'
                        _atomic_write_yaml(snapshot, incoming)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with _exclusive_lock(target):
                            preview = _merge_once(target, snapshot, apply=False)
                            print(f"预览：新增={preview.added}，更新={preview.updated}，"
                                  f"不变={preview.unchanged}，原有={preview.preserved}")
                            existing = _load_document(target, required=False)
                            merged, *_ = _merge_documents(existing, incoming, target=target, source=snapshot)
                            preview_changes(existing, merged, target)
                            if not preview.changed:
                                print("无需修改。")
                                continue
                            if input("确认应用？[y/N]：").strip().lower() not in {'y', 'yes'}:
                                print("已取消。")
                                continue
                            result = _merge_once(target, snapshot, apply=True)
                            print("已应用。" + ("已创建私有备份。" if result.backup else ""))
                elif choice == '4':
                    interactive_restore(target)
                else:
                    print("请选择 0–4。")
            except (OSError, TrustedNodesError) as exc:
                print(f"操作失败：{exc}", file=os.sys.stderr)
    except (EOFError, KeyboardInterrupt):
        print("\n已退出。")
        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="安全维护私有 trusted-nodes.yaml")
    parser.add_argument('--target', type=Path, help='目标 inventory；默认沿用节点生成器的路径')
    parser.add_argument('--import-dir', type=Path, help='导入文件目录；默认目标目录下的 imports')
    subparsers = parser.add_subparsers(dest="action")
    for name, help_text in [('list', '查看节点概要'), ('interactive', '打开交互菜单')]:
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument('--target', type=Path, default=argparse.SUPPRESS)
        if name == 'interactive':
            command.add_argument('--import-dir', type=Path, default=argparse.SUPPRESS)
    merge = subparsers.add_parser(
        "merge",
        help="预览或应用节点追加/按 id + 协议更新",
    )
    merge.add_argument("--target", type=Path, default=argparse.SUPPRESS, help="目标 trusted-nodes.yaml")
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
    remove = subparsers.add_parser(
        "remove",
        help="按稳定 id 删除全部协议，或用 --protocol 只删除一个协议",
    )
    remove.add_argument(
        "--target", type=Path, default=argparse.SUPPRESS, help="目标 trusted-nodes.yaml"
    )
    remove.add_argument("--id", required=True, help="要退役的稳定节点 id")
    remove.add_argument(
        "--protocol",
        choices=("vless", "hysteria2", "socks5"),
        help="只删除指定协议；省略时删除该 id 的全部协议",
    )
    remove.add_argument(
        "--apply",
        action="store_true",
        help="实际写入；省略时只预览，不创建备份或修改目标",
    )
    args = parser.parse_args(argv)
    if args.target is None:
        args.target = generator.default_trusted_nodes_file(
            generator.default_airport_dir(generator.default_hosts_dir())
        )
    args.target = args.target.expanduser()
    args.import_dir = (args.import_dir or Path(os.environ.get('CLASH_TRUSTED_IMPORT_DIR')
                                             or args.target.parent / 'imports')).expanduser()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.action in (None, 'interactive'):
            return interactive_menu(args.target, args.import_dir)
        if args.action == 'list':
            list_nodes(args.target)
            return 0
        if args.action == "merge":
            result = merge_trusted_nodes_file(
                args.target,
                args.source,
                apply=args.apply,
            )
        else:
            result = remove_trusted_nodes_file(
                args.target,
                args.id,
                protocol=args.protocol,
                apply=args.apply,
            )
    except (OSError, TrustedNodesError) as exc:
        print(f"trusted-nodes {args.action} 失败：{exc}", file=os.sys.stderr)
        return 1

    mode = "已应用" if args.apply else "预览"
    if args.action == "merge":
        print(
            f"trusted-nodes {mode}：新增={result.added}，更新={result.updated}，"
            f"不变={result.unchanged}，保留={result.preserved}，"
            f"changed={'yes' if result.changed else 'no'}"
        )
    else:
        print(
            f"trusted-nodes {mode}：删除={result.removed}，保留={result.preserved}，"
            f"changed={'yes' if result.changed else 'no'}"
        )
    if result.backup is not None:
        print("已创建目标文件备份；未输出节点内容。")
    elif not args.apply and result.changed:
        print("未写入目标；确认后追加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
