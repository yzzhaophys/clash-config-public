#!/usr/bin/env python3
"""Combine the Stash shell with static private nodes and close empty groups."""

from __future__ import annotations

import argparse
import copy
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml

from generate_stash_config import convert_config
from node_io import load_yaml


ROOT = Path(__file__).resolve().parent
DEFAULT_SHELL = ROOT / "home-stash.yaml"
DEFAULT_NODES = ROOT / "clash-vps.generated.yaml"
DEFAULT_HOME = ROOT / "home.yaml"
DEFAULT_OUTPUT = ROOT / "home-stash.private.yaml"
BUILTINS = {"DIRECT", "REJECT", "REJECT-DROP"}
AUTO_FIELDS = {
    "type", "proxies", "include-all", "filter", "exclude-filter", "use",
    "url", "interval", "lazy", "tolerance", "timeout", "expected-status",
    "max-failed-times", "strategy", "empty-fallback",
}
AUTO_TYPES = {"url-test", "fallback", "load-balance"}


def _groups_by_name(groups: Any, node_names: set[str]) -> dict[str, dict[str, Any]]:
    if not isinstance(groups, list):
        raise ValueError("最终配置的 proxy-groups 必须是列表")
    by_name: dict[str, dict[str, Any]] = {}
    for index, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get("name"), str) or not group["name"]:
            raise ValueError(f"proxy-groups[{index}] 缺少有效名称")
        name = group["name"]
        if name in by_name or name in node_names or name in BUILTINS:
            raise ValueError(f"proxy-groups[{index}] 名称重复或与节点冲突")
        by_name[name] = group

    for index, group in enumerate(groups):
        candidates = group.get("proxies", [])
        if not isinstance(candidates, list) or any(
            not isinstance(candidate, str) or not candidate for candidate in candidates
        ):
            raise ValueError(f"proxy-groups[{index}].proxies 必须是字符串列表")
        if any(candidate not in by_name and candidate not in node_names and candidate not in BUILTINS
               for candidate in candidates):
            raise ValueError(f"proxy-groups[{index}] 引用了不存在的代理")
        if group.get("use"):
            raise ValueError(f"proxy-groups[{index}] 使用动态 Provider，无法静态判断空组")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ValueError("最终配置的代理组存在循环引用")
        visiting.add(name)
        for candidate in by_name[name].get("proxies", []):
            if candidate in by_name:
                visit(candidate)
        visiting.remove(name)
        visited.add(name)

    for name in by_name:
        visit(name)
    return by_name


def _static_names(nodes: Any) -> tuple[list[dict[str, Any]], set[str]]:
    if not isinstance(nodes, dict) or set(nodes) != {"proxies"} or not isinstance(nodes["proxies"], list):
        raise ValueError("节点输入只能包含 proxies 列表")
    names: set[str] = set()
    for index, proxy in enumerate(nodes["proxies"]):
        if not isinstance(proxy, dict) or not isinstance(proxy.get("name"), str) or not proxy["name"]:
            raise ValueError(f"proxies[{index}] 缺少有效名称")
        if not isinstance(proxy.get("type"), str) or not proxy["type"]:
            raise ValueError(f"proxies[{index}] 缺少有效协议类型")
        if any(isinstance(key, str) and key.startswith("_") for key in proxy):
            raise ValueError(f"proxies[{index}] 含有不能导出的内部元数据")
        if proxy["name"] in names or proxy["name"] in BUILTINS:
            raise ValueError(f"proxies[{index}] 名称重复或与内置策略冲突")
        names.add(proxy["name"])
    proxies = copy.deepcopy(nodes["proxies"])
    for index, proxy in enumerate(proxies):
        if proxy["type"] == "hysteria2":
            password = proxy.get("password")
            auth = proxy.get("auth")
            if "password" in proxy and "auth" in proxy and password != auth:
                raise ValueError(f"proxies[{index}] 的 Hysteria2 认证字段冲突")
            credential = auth if "auth" in proxy else password
            if not isinstance(credential, str) or not credential:
                raise ValueError(f"proxies[{index}] 缺少有效的 Hysteria2 认证字段")
            proxy["auth"] = credential
            proxy.pop("password", None)
        elif proxy["type"] == "vless" and "servername" in proxy:
            servername = proxy["servername"]
            if not isinstance(servername, str):
                raise ValueError(f"proxies[{index}] 的 VLESS SNI 类型无效")
            if "sni" in proxy and proxy["sni"] != servername:
                raise ValueError(f"proxies[{index}] 的 VLESS SNI 字段冲突")
            proxy["sni"] = servername
            del proxy["servername"]
    return proxies, names


def _validate_dialer_graph(proxies: list[dict[str, Any]],
                           groups: dict[str, dict[str, Any]]) -> None:
    """Catch loops that cross a strategy group and a dialer-proxy node."""
    nodes = {proxy["name"]: proxy for proxy in proxies}
    active: set[tuple[str, str]] = set()
    visited: set[tuple[str, str]] = set()

    def visit(kind: str, name: str) -> None:
        key = (kind, name)
        if key in visited:
            return
        if key in active:
            raise ValueError("最终配置的代理链与策略组存在循环引用")
        active.add(key)
        if kind == "group":
            candidates = groups[name].get("proxies", [])
        else:
            dialer = nodes[name].get("dialer-proxy")
            candidates = [dialer] if dialer is not None else []
        for candidate in candidates:
            if candidate in groups:
                visit("group", candidate)
            elif candidate in nodes:
                visit("proxy", candidate)
        active.remove(key)
        visited.add(key)

    for name in groups:
        visit("group", name)
    for name in nodes:
        visit("proxy", name)


def _matches_static(group: dict[str, Any], names: list[str]) -> list[str]:
    if group.get("include-all") is not True:
        return []
    pattern = group.get("filter")
    if pattern is None:
        return list(names)
    if not isinstance(pattern, str):
        raise ValueError("自动组 filter 必须是字符串")
    try:
        compiled = re.compile(pattern)
    except re.error:
        raise ValueError("自动组 filter 不是有效正则") from None
    excluded = group.get("exclude-filter")
    if excluded is not None:
        if not isinstance(excluded, str):
            raise ValueError("自动组 exclude-filter 必须是字符串")
        try:
            excluded_regex = re.compile(excluded)
        except re.error:
            raise ValueError("自动组 exclude-filter 不是有效正则") from None
    else:
        excluded_regex = None
    return [name for name in names if compiled.search(name)
            and (excluded_regex is None or not excluded_regex.search(name))]


def _set_reject_only(group: dict[str, Any]) -> None:
    extras = {key: value for key, value in group.items() if key not in AUTO_FIELDS and key != "name"}
    name = group["name"]
    group.clear()
    group.update({"name": name, "type": "select", "proxies": ["REJECT"], "interval": -1})
    group.update(extras)


def build_private_config(shell: Any, nodes: Any, home: Any) -> tuple[dict[str, Any], int]:
    """Seal groups against the static node snapshot; never print node data."""
    if not isinstance(shell, dict) or shell.get("proxies") != []:
        raise ValueError("Stash 输入必须是尚未加入节点的配置骨架")
    if shell.get("proxy-providers") not in ({}, None):
        raise ValueError("动态代理提供者无法保证空组拒绝")
    if not isinstance(home, dict) or home.get("proxies") != [] or home.get("proxy-providers") not in ({}, None):
        raise ValueError("源 home 必须是无节点、无动态代理提供者的公开模板")
    expected = convert_config(home)
    if shell != expected:
        raise ValueError("公开 Stash 骨架与 home.yaml 不一致，请先运行 generate_stash_config.py")
    proxies, node_names = _static_names(nodes)
    ordered_names = [proxy["name"] for proxy in proxies]
    result = copy.deepcopy(shell)
    result["proxies"] = proxies
    groups = result.get("proxy-groups")
    by_name = _groups_by_name(groups, node_names)

    expected_groups = {group["name"]: group for group in expected["proxy-groups"]}
    source_groups = {group["name"]: group for group in home["proxy-groups"]}
    guarded = {
        group["name"] for group in home["proxy-groups"]
        if isinstance(group, dict) and group.get("empty-fallback") == "REJECT"
    }
    if not guarded or any(name not in by_name for name in guarded):
        raise ValueError("最终配置缺少源模板的空组保护目标")
    if any(group.get("include-all") is True or "filter" in group
           or "exclude-filter" in group for group in groups):
        raise ValueError("存在未审查的自动节点筛选组，无法保证空组拒绝")

    sealed: set[str] = set()
    for name in guarded:
        group = by_name[name]
        template = expected_groups[name]
        source_group = source_groups[name]
        if (group != template or group.get("type") != "url-test"
                or group.get("proxies", [])):
            raise ValueError("空组筛选与源模板不一致，无法可靠判断节点归属")
        matches = _matches_static(source_group, ordered_names)
        if not matches:
            _set_reject_only(group)
            sealed.add(name)
        else:
            # Private profiles contain a fixed node snapshot. Materialize its
            # members so Stash need not parse the large exclusion regexes.
            group["proxies"] = matches

    # An outer fallback with only empty children would otherwise inherit
    # Stash's DIRECT behavior.  Remove empty children when another route
    # exists; close the outer group too when no usable route remains.
    changed = True
    while changed:
        changed = False
        for group in groups:
            name = group["name"]
            if name in sealed:
                continue
            candidates = group.get("proxies", [])
            if not any(candidate in sealed for candidate in candidates):
                continue
            usable = [candidate for candidate in candidates
                      if candidate not in sealed and candidate != "REJECT"]
            if group.get("type") in AUTO_TYPES and usable:
                group["proxies"] = [candidate for candidate in candidates if candidate not in sealed]
                changed = True
            elif not usable and not _matches_static(group, ordered_names):
                _set_reject_only(group)
                sealed.add(name)
                changed = True

    _groups_by_name(groups, node_names)
    if any(group.get("type") in AUTO_TYPES and not group.get("proxies")
           and group.get("include-all") is not True for group in groups):
        raise ValueError("最终配置仍有未保护的空自动组")
    for index, proxy in enumerate(proxies):
        dialer = proxy.get("dialer-proxy")
        if "dialer-proxy" in proxy and (
            not isinstance(dialer, str)
            or dialer not in by_name and dialer not in node_names and dialer not in BUILTINS
        ):
            raise ValueError(f"proxies[{index}].dialer-proxy 引用无效")
    _validate_dialer_graph(proxies, by_name)
    return result, len(sealed)


def _check_paths(inputs: tuple[Path, ...], output: Path) -> None:
    destination = output.resolve()
    if destination.is_relative_to(ROOT) and destination != DEFAULT_OUTPUT.resolve():
        raise ValueError("仓库内含凭据的输出只能写入已忽略的 home-stash.private.yaml")
    if not output.parent.is_dir():
        raise ValueError("输出目录不存在")
    for source in inputs:
        if source.resolve() == destination:
            raise ValueError("输出不能覆盖输入文件")
        if output.exists() and os.path.samefile(source, output):
            raise ValueError("输出与输入文件存在硬链接冲突")
    if output.exists() or output.is_symlink():
        info = output.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or not stat.S_ISREG(info.st_mode):
            raise ValueError("拒绝覆盖符号链接、硬链接或非普通文件")


def write_private_config(shell_path: Path, nodes_path: Path, home_path: Path, output: Path) -> int:
    _check_paths((shell_path, nodes_path, home_path), output)
    config, sealed_count = build_private_config(
        load_yaml(shell_path), load_yaml(nodes_path), load_yaml(home_path)
    )
    try:
        rendered = (
            "# Private Stash profile. Contains credentials; do not commit.\n"
            + yaml.safe_dump(config, allow_unicode=True, sort_keys=False, width=120)
        )
    except yaml.YAMLError:
        raise ValueError("无法序列化私有 Stash 配置") from None

    fd, temporary_name = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        load_yaml(temporary)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return sealed_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shell", type=Path, default=DEFAULT_SHELL)
    parser.add_argument("--nodes", type=Path, default=DEFAULT_NODES)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    count = write_private_config(args.shell, args.nodes, args.home, args.output)
    print(f"已生成私有 Stash 配置: {args.output}；拒绝空组: {count}")


if __name__ == "__main__":
    main()
