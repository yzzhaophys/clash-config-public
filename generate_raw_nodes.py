#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
OUT = SCRIPT_DIR / "nodes.yaml"
FALLBACK_REGIONS = ("UK", "AU", "TW", "SG", "NL", "DE")

REGION_CN = {
    "hk": "香港",
    "us": "美国",
    "jp": "日本",
    "uk": "英国",
    "au": "澳洲",
    "tw": "台湾",
    "sg": "新加坡",
    "nl": "荷兰",
    "de": "德国",
}

REGION_CODE = {key: key.upper() for key in REGION_CN}


def load_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def cert_domain(path: str) -> str | None:
    match = re.search(r"/live/([^/]+)/", path)
    return match.group(1) if match else None


def port_from_listen(value: Any, default: int) -> int:
    text = str(value or "")
    match = re.search(r":(\d+)$", text)
    return int(match.group(1)) if match else default


def node_name(region: str, protocol: str, index: int) -> str:
    code = REGION_CODE.get(region.lower(), region.upper())
    cn = REGION_CN.get(region.lower(), code)
    proto = "H2" if protocol == "hysteria2" else "VLESS"
    return f"VPS-[{code}.Relay]-{proto}-{index:02d}-({cn}中转节点)"


def node_meta(name: str) -> dict[str, Any]:
    match = re.match(
        r"^VPS-\[(?P<region>[A-Z]+)\.(?P<role>[A-Za-z]+)\]-(?P<proto>[A-Z0-9]+)-(?P<idx>\d+)-\((?P<desc>[^)]+)\)(?:-\[Fallback=(?P<fallback>[^]]+)\])?$",
        name,
    )
    if not match:
        return {}
    return match.groupdict()


def anchor_name(name: str) -> str:
    meta = node_meta(name)
    if not meta:
        return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return f"VPS_{meta['region']}_{meta['role']}_{meta['proto']}_{meta['idx']}"


def yaml_scalar(value: Any) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[ " + " , ".join(yaml_scalar(item) for item in value) + " ]"
    if isinstance(value, dict):
        return "{ " + " , ".join(f"{key} : {yaml_scalar(item)}" for key, item in value.items()) + " }"
    return yaml_scalar(str(value))


def flow_map(items: list[tuple[str, Any]], anchor: str | None = None) -> str:
    prefix = f"&{anchor} " if anchor else ""
    return prefix + "{ " + " , ".join(f"{key} : {yaml_scalar(value)}" for key, value in items) + " }"


def ordered_items(proxy: dict[str, Any]) -> list[tuple[str, Any]]:
    order = [
        "name",
        "server",
        "port",
        "type",
        "uuid",
        "password",
        "encryption",
        "tls",
        "udp",
        "network",
        "skip-cert-verify",
        "alpn",
        "servername",
        "sni",
        "client-fingerprint",
        "reality-opts",
    ]
    items = [(key, proxy[key]) for key in order if key in proxy]
    items.extend((key, proxy[key]) for key in proxy if key not in order)
    return items


def xray_nodes(host_dir: Path, env: dict[str, str], counters: dict[tuple[str, str], int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    region = env.get("VPS_REGION", "xx").lower()
    xray_dir = host_dir / "config" / "xray"
    for file in sorted(xray_dir.glob("*.json")):
        try:
            data = json.loads(file.read_text())
        except Exception:
            continue
        for inbound in data.get("inbounds", []) or []:
            if inbound.get("protocol") != "vless":
                continue
            clients = inbound.get("settings", {}).get("clients", []) or []
            if not clients:
                continue
            client = clients[0]
            uuid = client.get("id")
            if not uuid:
                continue

            stream = inbound.get("streamSettings", {}) or {}
            security = stream.get("security")
            config: dict[str, Any] = {
                "type": "vless",
                "server": env.get("VPS_HOST", "0.0.0.0"),
                "port": int(inbound.get("port", 10000)),
                "uuid": uuid,
                "encryption": client.get("encryption", "none"),
                "network": stream.get("network", "tcp"),
                "tls": security in {"tls", "reality"},
                "udp": True,
            }

            if security == "tls":
                tls = stream.get("tlsSettings", {}) or {}
                servername = tls.get("serverName") or env.get("VPS_HOST", "")
                config["server"] = servername
                config["servername"] = servername
                config["skip-cert-verify"] = False
                if tls.get("alpn"):
                    config["alpn"] = tls["alpn"]
            elif security == "reality":
                reality = stream.get("realitySettings", {}) or {}
                names = reality.get("serverNames") or []
                servername = names[0] if isinstance(names, list) and names else env.get("VPS_HOST", "")
                config["servername"] = servername
                config["client-fingerprint"] = reality.get("fingerprint", "firefox")
                config["skip-cert-verify"] = False
                public_key = reality.get("publicKey")
                short_ids = reality.get("shortIds") or []
                short_id = short_ids[0] if isinstance(short_ids, list) and short_ids else ""
                if public_key:
                    config["reality-opts"] = {"public-key": public_key, "short-id": short_id}

            key = (region, "vless")
            idx = counters.get(key, 0)
            counters[key] = idx + 1
            out.append({"name": node_name(region, "vless", idx), **config})
    return out


def hy2_node(host_dir: Path, env: dict[str, str], counters: dict[tuple[str, str], int]) -> dict[str, Any] | None:
    path = host_dir / "config" / "hysteria" / "config.yaml"
    if not path.exists():
        return None
    data = yaml.safe_load(path.read_text()) or {}
    auth = data.get("auth", {}) or {}
    password = auth.get("password")
    if not password:
        return None
    tls = data.get("tls", {}) or {}
    server = cert_domain(str(tls.get("cert", ""))) or env.get("VPS_HOST", "0.0.0.0")
    region = env.get("VPS_REGION", "xx").lower()
    key = (region, "hysteria2")
    idx = counters.get(key, 0)
    counters[key] = idx + 1
    return {
        "name": node_name(region, "hysteria2", idx),
        "type": "hysteria2",
        "server": server,
        "port": port_from_listen(data.get("listen"), 20002),
        "password": password,
        "sni": server,
        "skip-cert-verify": False,
    }


def default_hosts_dir() -> Path:
    """优先从脚本目录读取 vps-*；若脚本放在 hosts 的独立子目录，则读取上一级。"""
    for candidate in (SCRIPT_DIR, SCRIPT_DIR.parent):
        if any(path.is_dir() and path.name != "vps-template" for path in candidate.glob("vps-*")):
            return candidate
    return SCRIPT_DIR


def collect_proxies(hosts_dir: Path) -> list[dict[str, Any]]:
    counters: dict[tuple[str, str], int] = {}
    proxies: list[dict[str, Any]] = []
    for host_dir in sorted(hosts_dir.glob("vps-*")):
        if host_dir.name == "vps-template":
            continue
        env = load_env(host_dir / "host.env")
        if not env:
            continue
        proxies.extend(xray_nodes(host_dir, env, counters))
        hy = hy2_node(host_dir, env, counters)
        if hy:
            proxies.append(hy)
    return proxies


def secure_write(output: Path, content: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content)
    output.chmod(0o600)


def write_plain(proxies: list[dict[str, Any]], output: Path) -> None:
    secure_write(output, yaml.safe_dump({"proxies": proxies}, allow_unicode=True, sort_keys=False))


def chain_name(exit_proxy: dict[str, Any], dialer: dict[str, Any]) -> str:
    exit_meta = node_meta(exit_proxy["name"])
    dialer_meta = node_meta(dialer["name"])
    exit_tag = exit_meta["region"]
    if exit_meta["role"] in {"HomeIP", "ShowIP"}:
        exit_tag += f".{exit_meta['role']}"
    return (
        f"PrxChain-[{exit_tag}]-{exit_meta['proto']}-{exit_meta['idx']}"
        f"--<<-{dialer_meta['region']}.{dialer_meta['role']}.{dialer_meta['proto']}.{dialer_meta['idx']}"
        f"-(代理链=={exit_meta['desc']}<-{dialer_meta['desc']})"
    )


def chain_candidates(proxies: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for exit_proxy in proxies:
        exit_meta = node_meta(exit_proxy["name"])
        if not exit_meta:
            continue
        # 地区补位节点不生成代理链；HomeIP/ShowIP 是落地出口，需要参与。
        if exit_meta.get("fallback") and exit_meta["role"] not in {"HomeIP", "ShowIP"}:
            continue
        for dialer in proxies:
            dialer_meta = node_meta(dialer["name"])
            if (
                not dialer_meta
                or dialer_meta.get("fallback")
                or dialer_meta["region"] == exit_meta["region"]
            ):
                continue
            dialer_label = f"{dialer_meta['region']}.{dialer_meta['proto']}.{dialer_meta['idx']}"
            if exit_meta.get("fallback") == dialer_label:
                continue
            candidates.append((exit_proxy, dialer))
    return candidates


def route_key(candidate: tuple[dict[str, Any], dict[str, Any]]) -> tuple[str, str]:
    exit_proxy, dialer = candidate
    exit_meta = node_meta(exit_proxy["name"])
    exit_tag = exit_meta["region"]
    if exit_meta["role"] in {"HomeIP", "ShowIP"}:
        exit_tag += f".{exit_meta['role']}"
    return exit_tag, node_meta(dialer["name"])["region"]


def parse_number_selection(raw: str, maximum: int) -> set[int]:
    value = raw.strip().lower()
    if value in {"all", "a", "全部"}:
        return set(range(maximum))
    if value in {"none", "n", "无", "不生成"}:
        return set()

    selected: set[int] = set()
    for part in value.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                start, end = end, start
            numbers = range(start, end + 1)
        else:
            numbers = [int(token)]
        for number in numbers:
            if number < 1 or number > maximum:
                raise ValueError(f"编号 {number} 超出范围 1-{maximum}")
            selected.add(number - 1)
    return selected


def prompt_number_selection(maximum: int) -> set[int]:
    while True:
        try:
            return parse_number_selection(input("请输入编号（如 1,3-5；all=全部；none=不选）："), maximum)
        except (ValueError, EOFError) as exc:
            print(f"输入无效：{exc}")


def exclude_by_patterns(proxies: list[dict[str, Any]], patterns: list[str]) -> list[dict[str, Any]]:
    if not patterns:
        return proxies
    try:
        compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    except re.error as exc:
        raise SystemExit(f"无效的 --exclude-node 正则表达式：{exc}") from exc
    kept = [proxy for proxy in proxies if not any(pattern.search(proxy["name"]) for pattern in compiled)]
    if not kept:
        raise SystemExit("过滤后没有剩余节点")
    return kept


def interactive_exclude_nodes(proxies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    print(f"\n发现 {len(proxies)} 个基础节点：")
    for index, proxy in enumerate(proxies, 1):
        print(f"  {index:>2}. {proxy['name']}")
    while True:
        try:
            raw = input(
                "要剔除哪些基础节点？输入编号/范围（如 2,5-6）；直接回车或 none 表示不剔除："
            )
            excluded = parse_number_selection(raw or "none", len(proxies))
        except ValueError as exc:
            print(f"输入无效：{exc}")
            continue
        kept = [proxy for index, proxy in enumerate(proxies) if index not in excluded]
        if not kept:
            print("不能剔除全部基础节点，请重新选择。")
            continue
        if excluded:
            print(f"已剔除 {len(excluded)} 个节点，剩余 {len(kept)} 个；相关代理链也不会生成。")
        return kept


def fallback_node(source: dict[str, Any], target_region: str) -> dict[str, Any]:
    source_meta = node_meta(source["name"])
    if not source_meta:
        raise ValueError(f"无法识别备用来源节点名称：{source['name']}")
    target_cn = REGION_CN.get(target_region.lower(), target_region)
    source_label = f"{source_meta['region']}.{source_meta['proto']}.{source_meta['idx']}"
    node = copy.deepcopy(source)
    node["name"] = (
        f"VPS-[{target_region}.Relay]-{source_meta['proto']}-{source_meta['idx']}"
        f"-({target_cn}中转节点)-[Fallback={source_label}]"
    )
    return node


def special_role_node(source: dict[str, Any], role: str) -> dict[str, Any]:
    source_meta = node_meta(source["name"])
    if not source_meta:
        raise ValueError(f"无法识别特殊角色来源节点名称：{source['name']}")
    descriptions = {
        "HomeIP": "美国住宅节点",
        "ShowIP": "归属地落地节点",
    }
    source_label = f"{source_meta['region']}.{source_meta['proto']}.{source_meta['idx']}"
    node = copy.deepcopy(source)
    node["name"] = (
        f"VPS-[US.{role}]-{source_meta['proto']}-{source_meta['idx']}"
        f"-({descriptions[role]})-[Fallback={source_label}]"
    )
    return node


def prompt_source_node(proxies: list[dict[str, Any]], target_region: str | None = None) -> dict[str, Any] | None:
    suffix = f"（{target_region}）" if target_region else ""
    print(f"\n可选的备用来源节点{suffix}：")
    for index, proxy in enumerate(proxies, 1):
        print(f"  {index:>2}. {proxy['name']}")
    while True:
        raw = input(f"请选择备用来源节点{suffix} [1-{len(proxies)}；0=跳过]：").strip()
        if raw in {"", "0", "none", "n"}:
            return None
        try:
            number = int(raw)
        except ValueError:
            print("输入无效，请输入节点编号。")
            continue
        if 1 <= number <= len(proxies):
            return proxies[number - 1]
        print(f"编号超出范围 1-{len(proxies)}。")


def interactive_fallback_nodes(proxies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    real_regions = {
        meta["region"]
        for proxy in proxies
        if (meta := node_meta(proxy["name"])) and not meta.get("fallback")
    }
    missing = [region for region in FALLBACK_REGIONS if region not in real_regions]
    if not missing:
        return []

    print("\n以下地区没有真实节点：" + ", ".join(missing))
    print("可生成逻辑占位节点，避免空策略组回退到 COMPATIBLE/直连。")
    print("占位节点的实际出口仍是所选来源节点，并且不会参与代理链组合。")
    print("  1. 不生成占位节点")
    print("  2. 所有缺失地区使用同一个来源节点（推荐当前用法）")
    print("  3. 每个缺失地区分别选择来源节点")
    while True:
        choice = input("请选择 [1-3，默认 2]：").strip() or "2"
        if choice in {"1", "2", "3"}:
            break
        print("输入无效，请选择 1、2 或 3。")
    if choice == "1":
        return []

    placeholders: list[dict[str, Any]] = []
    if choice == "2":
        source = prompt_source_node(proxies)
        if source is None:
            return []
        placeholders = [fallback_node(source, region) for region in missing]
    else:
        for region in missing:
            source = prompt_source_node(proxies, region)
            if source is not None:
                placeholders.append(fallback_node(source, region))
    if placeholders:
        print(f"已生成 {len(placeholders)} 个地区占位节点。")
    return placeholders


def interactive_special_role_nodes(proxies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    print("\nHomeIP / ShowIP 逻辑节点：")
    print("这只是将所选节点放入对应策略组，不会把普通 VPS 变成住宅 IP。")
    print("  1. 同一个来源同时作为 HomeIP 和 ShowIP（默认）")
    print("  2. HomeIP 和 ShowIP 分别选择来源")
    print("  3. 不生成")
    while True:
        choice = input("请选择 [1-3，默认 1]：").strip() or "1"
        if choice in {"1", "2", "3"}:
            break
        print("输入无效，请选择 1、2 或 3。")
    if choice == "3":
        return []

    aliases: list[dict[str, Any]] = []
    if choice == "1":
        source = prompt_source_node(proxies, "HomeIP + ShowIP")
        if source is not None:
            aliases = [special_role_node(source, role) for role in ("HomeIP", "ShowIP")]
    else:
        for role in ("HomeIP", "ShowIP"):
            source = prompt_source_node(proxies, role)
            if source is not None:
                aliases.append(special_role_node(source, role))
    if aliases:
        print(f"已生成 {len(aliases)} 个 HomeIP/ShowIP 逻辑节点。")
    return aliases


def interactive_selection(
    proxies: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    str,
    list[tuple[dict[str, Any], dict[str, Any]]],
    list[dict[str, Any]],
]:
    proxies = interactive_exclude_nodes(proxies)
    print("\n输出格式：")
    print("  1. Clash Verge Rev 扩展配置（proxies 覆写，推荐）")
    print("  2. 主配置 proxies 模板（可直接写入主配置）")
    print("  3. 仅基础节点 YAML（不生成代理链）")
    while True:
        choice = input("请选择 [1-3，默认 1]：").strip() or "1"
        if choice in {"1", "2", "3"}:
            break
        print("输入无效，请选择 1、2 或 3。")
    output_format = {"1": "merge", "2": "template", "3": "plain"}[choice]
    if output_format == "plain":
        return proxies, output_format, [], []

    special_nodes = interactive_special_role_nodes(proxies)
    candidates = chain_candidates(proxies + special_nodes)

    print(f"\n可生成 {len(candidates)} 条代理链：")
    print("  1. 全部生成")
    print("  2. 按出口地区 <- 入口地区选择")
    print("  3. 逐条选择")
    print("  4. 不生成代理链")
    while True:
        choice = input("请选择 [1-4，默认 2]：").strip() or "2"
        if choice in {"1", "2", "3", "4"}:
            break
        print("输入无效，请选择 1、2、3 或 4。")

    if choice == "1":
        return proxies, output_format, candidates, special_nodes
    if choice == "4":
        return proxies, output_format, [], special_nodes
    if choice == "2":
        routes: list[tuple[str, str]] = []
        counts: dict[tuple[str, str], int] = {}
        for candidate in candidates:
            key = route_key(candidate)
            if key not in counts:
                routes.append(key)
                counts[key] = 0
            counts[key] += 1
        print("\n代理链方向（出口 <- 入口）：")
        for index, key in enumerate(routes, 1):
            print(f"  {index:>2}. {key[0]} <- {key[1]}（{counts[key]} 条）")
        selected_routes = {routes[index] for index in prompt_number_selection(len(routes))}
        return (
            proxies,
            output_format,
            [candidate for candidate in candidates if route_key(candidate) in selected_routes],
            special_nodes,
        )

    print("\n具体代理链：")
    for index, candidate in enumerate(candidates, 1):
        print(f"  {index:>2}. {chain_name(*candidate)}")
    selected = prompt_number_selection(len(candidates))
    return (
        proxies,
        output_format,
        [candidate for index, candidate in enumerate(candidates) if index in selected],
        special_nodes,
    )


def select_routes(
    proxies: list[dict[str, Any]], route_spec: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    candidates = chain_candidates(proxies)
    available = {route_key(candidate) for candidate in candidates}
    requested: set[tuple[str, str]] = set()
    for raw in route_spec.split(","):
        token = raw.strip().upper().replace(" ", "")
        if not token:
            continue
        separator = "<-" if "<-" in token else ":"
        if separator not in token:
            raise SystemExit(f"无效方向 {raw!r}；请使用 'HK<-JP,US<-HK' 或 'HK:JP,US:HK'")
        exit_region, dialer_region = token.split(separator, 1)
        key = (exit_region, dialer_region)
        if key not in available:
            raise SystemExit(f"没有可用的代理链方向：{exit_region} <- {dialer_region}")
        requested.add(key)
    return [candidate for candidate in candidates if route_key(candidate) in requested]


def write_template(
    proxies: list[dict[str, Any]],
    chains: list[tuple[dict[str, Any], dict[str, Any]]],
    output: Path,
) -> None:
    # Clash Verge Rev 1.7+ 的 YAML 扩展配置使用字段覆写；
    # prepend/append 已移到订阅的可视化编辑功能，不再输出旧式 prepend-proxies。
    lines = ["proxies:"]
    current_region = None

    for proxy in proxies:
        meta = node_meta(proxy["name"])
        region = meta.get("region", "XX")
        if region != current_region:
            lines.extend(
                [
                    "",
                    "  ###  --------------------------------------------------------------------",
                    f"  ###  {region} - VPS nodes",
                    "  ###  --------------------------------------------------------------------",
                    "",
                ]
            )
            current_region = region
        lines.append(f"    - {flow_map(ordered_items(proxy), anchor_name(proxy['name']))}")

    lines.extend(["", "", "###### START ClashMeta_Only proxy chains", ""])

    selected_names = {chain_name(*candidate) for candidate in chains}
    for exit_proxy in proxies:
        exit_meta = node_meta(exit_proxy["name"])
        if not exit_meta:
            continue
        exit_chains = [
            candidate
            for candidate in chains
            if candidate[0]["name"] == exit_proxy["name"] and chain_name(*candidate) in selected_names
        ]
        if not exit_chains:
            continue
        lines.extend(
            [
                f"  ###  {exit_meta['region']}.{exit_meta['role']} {exit_meta['proto']}-{exit_meta['idx']} exit",
                "",
            ]
        )
        for _, dialer in exit_chains:
            lines.append(
                "    - "
                + "{ "
                + f"name : {yaml_scalar(chain_name(exit_proxy, dialer))} , "
                + f"<< : *{anchor_name(exit_proxy['name'])} , "
                + f"dialer-proxy : {yaml_scalar(dialer['name'])}"
                + " }"
            )
        lines.append("")

    lines.append("###### END ClashMeta_Only proxy chains")
    secure_write(output, "\n".join(lines).rstrip() + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从各 VPS 本地配置生成 Mihomo 节点和代理链")
    parser.add_argument(
        "--hosts-dir",
        type=Path,
        default=default_hosts_dir(),
        help="包含 vps-* 私有配置目录（默认自动检测脚本目录或上一级）",
    )
    parser.add_argument("--output", "-o", type=Path, default=OUT, help="输出文件")
    parser.add_argument("--raw-output", type=Path, help="额外输出仅含基础节点的 proxies YAML")
    formats = parser.add_mutually_exclusive_group()
    formats.add_argument("--plain", action="store_true", help="仅输出基础节点（默认）")
    formats.add_argument("--template", action="store_true", help="输出 proxies 模板和代理链")
    formats.add_argument(
        "--merge",
        action="store_true",
        help="输出 Clash Verge Rev 扩展配置（proxies 覆写）",
    )
    parser.add_argument("--interactive", "-i", action="store_true", help="交互选择输出格式和代理链")
    parser.add_argument("--chains", choices=("all", "none"), default="all", help="非交互模式生成全部或不生成代理链")
    parser.add_argument(
        "--exclude-node",
        action="append",
        default=[],
        metavar="REGEX",
        help="按节点名称正则剔除基础节点，可重复使用；相关代理链也会被剔除",
    )
    parser.add_argument(
        "--routes",
        help="仅生成指定方向，例如 'HK<-JP,US<-HK'；也可写成 'HK:JP,US:HK'",
    )
    return parser.parse_args(argv)


def apply_default_invocation(args: argparse.Namespace, invoked_without_args: bool) -> None:
    if not invoked_without_args:
        return
    args.interactive = True
    args.output = SCRIPT_DIR / "clash-vps.generated.yaml"
    args.raw_output = SCRIPT_DIR / "nodes.yaml"


def main(argv: list[str] | None = None) -> int:
    invoked_without_args = len(sys.argv) == 1 if argv is None else len(argv) == 0
    args = parse_args(argv)
    apply_default_invocation(args, invoked_without_args)
    hosts_dir = args.hosts_dir.expanduser().resolve()
    proxies = collect_proxies(hosts_dir)
    if not proxies:
        raise SystemExit(
            f"没有在 {hosts_dir} 找到节点；请检查 vps-*/host.env 和本地服务配置"
        )

    fallback_nodes: list[dict[str, Any]] = []
    if args.interactive:
        proxies, output_format, chains, special_nodes = interactive_selection(proxies)
        if output_format != "plain":
            fallback_nodes = interactive_fallback_nodes(proxies)
            fallback_nodes.extend(special_nodes)
    else:
        before = len(proxies)
        proxies = exclude_by_patterns(proxies, args.exclude_node)
        if len(proxies) != before:
            print(f"excluded {before - len(proxies)} base nodes, {len(proxies)} remaining")
        output_format = "merge" if args.merge else "template" if args.template else "plain"
        if output_format == "plain" or args.chains == "none":
            chains = []
        elif args.routes:
            chains = select_routes(proxies, args.routes)
        else:
            chains = chain_candidates(proxies)

    if output_format == "plain":
        write_plain(proxies, args.output)
    else:
        write_template(proxies + fallback_nodes, chains, args.output)
    print(
        f"wrote {args.output} ({len(proxies)} base nodes, {len(fallback_nodes)} fallback nodes, "
        f"{len(chains)} chains, format={output_format})"
    )
    if args.raw_output:
        write_plain(proxies, args.raw_output)
        print(f"wrote {args.raw_output} ({len(proxies)} base nodes, raw=True)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
