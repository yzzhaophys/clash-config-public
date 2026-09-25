#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml
from node_io import load_yaml
from node_conversion import xray_options, hysteria_options


SCRIPT_DIR = Path(__file__).resolve().parent
OUT = SCRIPT_DIR / "nodes.yaml"
LOON_OUT = SCRIPT_DIR / "loon-nodes.conf"
HOME_TEMPLATE = SCRIPT_DIR / "home.yaml"
AIRPORT_REGION_ALIASES = {"GB": "UK"}
PROXY_GROUP_BUILTINS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS"}

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
REGION_ALIASES = {
    "hong kong": "hk",
    "hongkong": "hk",
    "japan": "jp",
    "united states": "us",
    "united states of america": "us",
    "usa": "us",
    "united kingdom": "uk",
    "great britain": "uk",
    "england": "uk",
    "gb": "uk",
    "australia": "au",
    "taiwan": "tw",
    "singapore": "sg",
    "malaysia": "my",
    "netherlands": "nl",
    "the netherlands": "nl",
    "germany": "de",
    "france": "fr",
    "canada": "ca",
}
AIRPORT_REGION_PRIORITY = ("HK", "TW", "SG", "JP", "US", "UK", "AU", "DE", "NL")
DEFAULT_CORE_REGIONS = {"hk", "jp", "sg"}


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


CLASH_HOST_VAR_MAP = {
    "vps_clash_region": "VPS_CLASH_REGION",
    "vps_clash_order": "VPS_CLASH_ORDER",
    "vps_clash_allow_relay": "VPS_CLASH_ALLOW_RELAY",
    "vps_clash_allow_direct_exit": "VPS_CLASH_ALLOW_DIRECT_EXIT",
    "vps_clash_allow_chain_exit": "VPS_CLASH_ALLOW_CHAIN_EXIT",
    "vps_clash_exit_type": "VPS_CLASH_EXIT_TYPE",
    "vps_clash_allow_showip": "VPS_CLASH_ALLOW_SHOWIP",
    "vps_clash_allow_download": "VPS_CLASH_ALLOW_DOWNLOAD",
    "vps_clash_relay_protocol": "VPS_CLASH_RELAY_PROTOCOL",
    "vps_clash_chain_exit_protocol": "VPS_CLASH_CHAIN_EXIT_PROTOCOL",
}


def load_ansible_clash_vars(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = load_yaml(path) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: Ansible host_vars 不是有效 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: Ansible host_vars 必须是映射")
    result: dict[str, str] = {}
    for source_key, target_key in CLASH_HOST_VAR_MAP.items():
        if source_key not in data:
            continue
        value = data[source_key]
        if isinstance(value, bool):
            result[target_key] = "true" if value else "false"
        else:
            result[target_key] = str(value)
    return result


def cert_domain(path: str) -> str | None:
    match = re.search(r"/live/([^/]+)/", path)
    return match.group(1) if match else None


def parse_port(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是 1-65535 的整数")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field} 必须是 1-65535 的整数")
    if isinstance(value, str) and not value.strip().isdigit():
        raise ValueError(f"{field} 必须是 1-65535 的整数")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是 1-65535 的整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{field} 必须是 1-65535 的整数")
    return port


def port_from_listen(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, (int, str)) and str(value).strip().isdigit():
        return parse_port(value, "listen")
    text = str(value).strip()
    match = re.search(r":(\d+)(?:-\d+)?$", text)
    if not match:
        raise ValueError(f"listen 必须包含有效端口，当前值为 {value!r}")
    return parse_port(match.group(1), "listen")


def normalize_region(region: str) -> str:
    normalized = re.sub(r"[_-]+", " ", str(region or "").strip().lower())
    normalized = re.sub(r"\s+", " ", normalized)
    return REGION_ALIASES.get(normalized, normalized)


def env_bool(env: dict[str, str], key: str, default: bool) -> bool:
    value = env.get(key)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{key} 必须是 true 或 false，当前值为 {value!r}")


def yaml_bool(value: Any, field: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise ValueError(f"{field} 必须是 true 或 false，当前值为 {value!r}")


def require_nonempty_string(
    value: Any,
    field: str,
    *,
    trim: bool = True,
) -> str:
    """Return a validated scalar string without coercing YAML null/containers."""
    if not isinstance(value, str) or not (value.strip() if trim else value):
        raise ValueError(f"{field} 必须是非空字符串")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{field} 不能包含换行")
    return value.strip() if trim else value


def require_socks5_credentials(proxy: dict[str, Any], field: str) -> None:
    """Require credentials for optional SOCKS5 nodes instead of open proxies."""
    for key in ("username", "password"):
        try:
            value = require_nonempty_string(proxy.get(key), f"{field}.{key}", trim=False)
        except ValueError as exc:
            raise ValueError(
                f"{field}.{key} 必须填写；SOCKS5 节点不能使用匿名认证"
            ) from exc
        proxy[key] = value


def require_proxy_credentials(
    proxy: dict[str, Any],
    protocol: str,
    field: str,
    *,
    authenticated_socks5: bool = False,
) -> None:
    """Validate credentials required to produce a usable client node."""
    if protocol in {"vless", "vmess"}:
        proxy["uuid"] = require_nonempty_string(proxy.get("uuid"), f"{field}.uuid", trim=False)
    elif protocol in {"hysteria2", "trojan", "ss"}:
        proxy["password"] = require_nonempty_string(
            proxy.get("password"),
            f"{field}.password",
            trim=False,
        )
        if protocol == "ss":
            proxy["cipher"] = require_nonempty_string(
                proxy.get("cipher"), f"{field}.cipher", trim=False,
            )
    elif protocol == "socks5" and authenticated_socks5:
        require_socks5_credentials(proxy, field)


def host_capabilities(host_dir: Path, env: dict[str, str]) -> dict[str, Any]:
    raw_region = env.get("VPS_CLASH_REGION", env.get("VPS_REGION", ""))
    region = normalize_region(raw_region)
    if region == "xx" or not re.fullmatch(r"[a-z]{2}", region):
        raise ValueError(
            "VPS_CLASH_REGION 必须是两位地区代码或已知地区名称，"
            f"当前值为 {raw_region!r}"
        )
    exit_type = env.get("VPS_CLASH_EXIT_TYPE", "general").strip().lower() or "general"
    if exit_type not in {"general", "homeip"}:
        raise ValueError(
            "VPS_CLASH_EXIT_TYPE 必须是 general 或 homeip，"
            f"当前值为 {exit_type!r}"
        )
    allow_relay = env_bool(
        env,
        "VPS_CLASH_ALLOW_RELAY",
        exit_type == "general" and region in DEFAULT_CORE_REGIONS,
    )
    if exit_type == "homeip" and allow_relay:
        raise ValueError("HomeIP 节点不能设置 VPS_CLASH_ALLOW_RELAY=true")
    relay_protocol = env.get("VPS_CLASH_RELAY_PROTOCOL", "vless").strip().lower()
    chain_exit_protocol = env.get("VPS_CLASH_CHAIN_EXIT_PROTOCOL", "vless").strip().lower()
    allowed_protocols = {"vless", "hysteria2"}
    if relay_protocol not in allowed_protocols:
        raise ValueError("VPS_CLASH_RELAY_PROTOCOL 必须是 vless 或 hysteria2")
    if chain_exit_protocol not in allowed_protocols:
        raise ValueError("VPS_CLASH_CHAIN_EXIT_PROTOCOL 必须是 vless 或 hysteria2")
    return {
        "region": region,
        "allow_relay": allow_relay,
        "allow_direct_exit": env_bool(env, "VPS_CLASH_ALLOW_DIRECT_EXIT", True),
        "allow_chain_exit": env_bool(env, "VPS_CLASH_ALLOW_CHAIN_EXIT", True),
        "allow_download": env_bool(env, "VPS_CLASH_ALLOW_DOWNLOAD", False),
        "allow_showip": env_bool(env, "VPS_CLASH_ALLOW_SHOWIP", False),
        "exit_type": exit_type,
        "physical_node_id": host_dir.name,
        "relay_protocol": relay_protocol,
        "chain_exit_protocol": chain_exit_protocol,
    }


def capability_role(capabilities: dict[str, Any]) -> str:
    if capabilities["exit_type"] == "homeip":
        return "HomeIP"
    return "Core" if capabilities["allow_relay"] else "Exit"


def attach_capabilities(proxy: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any]:
    proxy["_allow-relay"] = capabilities["allow_relay"]
    proxy["_allow-direct-exit"] = capabilities["allow_direct_exit"]
    proxy["_allow-chain-exit"] = capabilities["allow_chain_exit"]
    proxy["_allow-download"] = capabilities["allow_download"]
    proxy["_allow-showip"] = capabilities["allow_showip"]
    proxy["_exit-type"] = capabilities["exit_type"]
    proxy["_physical-node-id"] = capabilities["physical_node_id"]
    proxy["_relay-protocol"] = capabilities["relay_protocol"]
    proxy["_chain-exit-protocol"] = capabilities["chain_exit_protocol"]
    return proxy


def capability_name_suffix(
    allow_direct_exit: bool,
    allow_download: bool,
    allow_showip: bool = False,
) -> str:
    suffix = ""
    if not allow_direct_exit:
        suffix += "-[Direct=false]"
    if allow_download:
        suffix += "-[Download=true]"
    if allow_showip:
        suffix += "-[ShowIP=true]"
    return suffix


def node_name(
    region: str,
    protocol: str,
    index: int,
    role: str = "Exit",
    *,
    allow_direct_exit: bool = True,
    allow_download: bool = False,
    allow_showip: bool = False,
) -> str:
    region = normalize_region(region)
    code = REGION_CODE.get(region, region.upper())
    cn = REGION_CN.get(region, code)
    proto = {
        "vless": "VLESS",
        "hysteria2": "H2",
        "socks5": "SOCKS5",
    }.get(protocol, protocol.upper() or "NODE")
    descriptions = {
        "Core": f"{cn}核心节点",
        "Exit": f"{cn}出口节点",
        "HomeIP": f"{cn}住宅节点",
    }
    description = descriptions.get(role, cn + "节点")
    return (
        f"VPS-[{code}.{role}]-{proto}-{index:02d}-({description})"
        + capability_name_suffix(allow_direct_exit, allow_download, allow_showip)
    )


def node_meta(name: str) -> dict[str, Any]:
    match = re.match(
        r"^VPS-\[(?P<region>[A-Z]+)\.(?P<role>[A-Za-z]+)\]-(?P<proto>[A-Z0-9]+)-(?P<idx>\d+)-\((?P<desc>[^)]+)\)(?:-\[Source=(?P<source>[^]]+)\])?(?:-\[Airport=(?P<airport>[^]]+)\])?(?:-\[Special=(?P<special>[^]]+)\])?(?:-\[Direct=(?P<direct>[^]]+)\])?(?:-\[Download=(?P<download>[^]]+)\])?(?:-\[ShowIP=(?P<showip>[^]]+)\])?(?:-\[Trusted=(?P<trusted>[^]]+)\])?$",
        name,
    )
    if not match:
        return {}
    return match.groupdict()


def load_home_proxy_groups(path: Path = HOME_TEMPLATE) -> dict[str, dict[str, Any]]:
    """Load and validate the public group graph used by template output."""

    try:
        source = load_yaml(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: 无法读取 home 模板") from exc
    if not isinstance(source, dict):
        raise ValueError(f"{path}: home 模板顶层必须是映射")
    groups = source.get("proxy-groups")
    if not isinstance(groups, list):
        raise ValueError(f"{path}: proxy-groups 必须是列表")

    by_name: dict[str, dict[str, Any]] = {}
    references: dict[str, list[str]] = {}
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError(f"{path}: proxy-groups[{index}] 必须是映射")
        name = group.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{path}: proxy-groups[{index}].name 必须是非空字符串")
        if name in by_name:
            raise ValueError(f"{path}: proxy-groups 存在重复名称 {name}")
        by_name[name] = group

        for field in ("filter", "exclude-filter"):
            if field not in group:
                continue
            value = group[field]
            if not isinstance(value, str):
                raise ValueError(f"{path}: 代理组 {name} 的 {field} 必须是字符串")
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"{path}: 代理组 {name} 的 {field} 不是有效正则") from exc

        candidates = group.get("proxies", [])
        if not isinstance(candidates, list):
            raise ValueError(f"{path}: 代理组 {name} 的 proxies 必须是列表")
        if any(not isinstance(candidate, str) or not candidate for candidate in candidates):
            raise ValueError(f"{path}: 代理组 {name} 的 proxies 必须只包含非空字符串")
        references[name] = candidates

    for name, candidates in references.items():
        for candidate in candidates:
            if candidate not in by_name and candidate not in PROXY_GROUP_BUILTINS:
                raise ValueError(f"{path}: 代理组 {name} 引用了不存在的代理组 {candidate}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"{path}: 代理组存在循环引用 {name}")
        visiting.add(name)
        for candidate in references[name]:
            if candidate in by_name:
                visit(candidate)
        visiting.remove(name)
        visited.add(name)

    for name in by_name:
        visit(name)
    return by_name


def _group_matches_proxy(group: dict[str, Any], proxy_name: str) -> bool:
    expression = group.get("filter")
    if not isinstance(expression, str) or re.search(expression, proxy_name) is None:
        return False
    excluded = group.get("exclude-filter")
    return not isinstance(excluded, str) or re.search(excluded, proxy_name) is None


def _group_with_suffix(
    groups: dict[str, dict[str, Any]], suffix: str
) -> dict[str, Any] | None:
    matches = [group for name, group in groups.items() if name.endswith(suffix)]
    if len(matches) > 1:
        names = ", ".join(str(group.get("name")) for group in matches)
        raise ValueError(f"home 模板中存在重复的组后缀 {suffix}: {names}")
    return matches[0] if matches else None


def _check_optional_group_match(
    groups: dict[str, dict[str, Any]],
    suffix: str,
    proxy_name: str,
    *,
    expected: bool,
    label: str,
) -> None:
    group = _group_with_suffix(groups, suffix)
    if group is None or "filter" not in group:
        return
    actual = _group_matches_proxy(group, proxy_name)
    if actual != expected:
        raise ValueError(
            f"生成节点 {proxy_name} 与 home 模板的 {label} 筛选不一致: {group['name']}"
        )


def validate_generated_against_home(
    proxies: list[dict[str, Any]],
    chains: list[tuple[dict[str, Any], dict[str, Any]]],
    home_template: Path = HOME_TEMPLATE,
) -> None:
    """Check generated names against the active home.yaml filters and groups."""

    groups = load_home_proxy_groups(home_template)
    for proxy in proxies:
        name = proxy.get("name")
        if not isinstance(name, str) or not node_meta(name):
            raise ValueError("生成节点名称无法按 home 模板解析")
        meta = node_meta(name)
        region = meta["region"]
        role = meta["role"]
        direct_tag = f"{region}.HomeIP" if role == "HomeIP" else region
        _check_optional_group_match(
            groups,
            f".DirectExit-[{direct_tag}]",
            name,
            expected=bool(proxy.get("_allow-direct-exit", True)),
            label="DirectExit",
        )
        if proxy.get("_allow-showip", False):
            # ShowIP is independent of single-node direct exit. A ShowIP node
            # enters the direct-exit subgroup only when direct exit is allowed;
            # chain eligibility is checked separately on generated chains.
            _check_optional_group_match(
                groups,
                f".DirectExit-[{region}.ShowIP]",
                name,
                expected=bool(proxy.get("_allow-direct-exit", True)),
                label="ShowIP DirectExit",
            )
        if proxy.get("_allow-download", False):
            _check_optional_group_match(
                groups,
                ".DirectExit-[Download]",
                name,
                expected=True,
                label="Download",
            )

    for candidate in chains:
        if not isinstance(candidate, tuple) or len(candidate) != 2:
            raise ValueError("生成代理链候选项格式无效")
        exit_proxy, dialer = candidate
        exit_name = exit_proxy.get("name")
        dialer_name = dialer.get("name")
        exit_meta = node_meta(exit_name) if isinstance(exit_name, str) else {}
        if not exit_meta or not isinstance(dialer_name, str) or not node_meta(dialer_name):
            raise ValueError("生成代理链包含无法按 home 模板解析的节点")
        chain = chain_name(exit_proxy, dialer)
        exit_tag = exit_meta["region"]
        if exit_meta["role"] == "HomeIP":
            exit_tag += ".HomeIP"
        chain_group = _group_with_suffix(groups, f".Chain-[{exit_tag}]")
        if chain_group is None or "filter" not in chain_group:
            raise ValueError(f"home 模板缺少代理链组: .Chain-[{exit_tag}]")
        if not _group_matches_proxy(chain_group, chain):
            raise ValueError(
                f"生成代理链 {chain} 未命中 home 模板筛选: {chain_group['name']}"
            )
        if exit_proxy.get("_allow-showip", False):
            show_group = _group_with_suffix(
                groups, f".Chain-[{exit_meta['region']}.ShowIP]"
            )
            if show_group is None or "filter" not in show_group:
                raise ValueError(
                    f"home 模板缺少 ShowIP 代理链组: .Chain-[{exit_meta['region']}.ShowIP]"
                )
            if not _group_matches_proxy(show_group, chain):
                raise ValueError(
                    f"生成 ShowIP 代理链 {chain} 未命中 home 模板筛选: {show_group['name']}"
                )


def source_marker_label(value: Any) -> str:
    """Make a source name safe to nest inside an ASCII marker."""
    label = re.sub(r"[\r\n]+", " ", str(value).strip())
    return label.replace("[", "［").replace("]", "］")


def anchor_name(name: str) -> str:
    meta = node_meta(name)
    if not meta:
        return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    base = f"VPS_{meta['region']}_{meta['role']}_{meta['proto']}_{meta['idx']}"
    # Airport and trusted nodes can intentionally reuse a base
    # region/protocol/index. Include their source marker so YAML anchors stay
    # unique even when a future caller does not share counters.
    markers: list[str] = []
    for field in ("source", "airport", "special", "trusted"):
        value = meta.get(field)
        if not value:
            continue
        token = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_") or "value"
        markers.append(f"{field.title()}_{token}")
    return base + ("_" + "_".join(markers) if markers else "")


def ensure_unique_anchors(proxies: list[dict[str, Any]]) -> None:
    seen: dict[str, str] = {}
    for proxy in proxies:
        name = str(proxy.get("name", "<unnamed>"))
        anchor = anchor_name(name)
        previous = seen.get(anchor)
        if previous is not None:
            raise ValueError(
                f"节点 YAML anchor 重复：{anchor}（{previous!r} 与 {name!r}）；"
                "请检查节点来源和编号"
            )
        seen[anchor] = name


def yaml_scalar(value: Any) -> str:
    if isinstance(value, str):
        # JSON strings are valid YAML scalars and correctly escape controls,
        # quotes, and newlines from imported subscription data.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        # Use YAML's float spelling, including exponents and non-finite values.
        # Python's str(1e-7) is read as a string by our YAML 1.1 loader.
        return yaml.safe_dump(value, default_flow_style=True).splitlines()[0]
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[ " + " , ".join(yaml_scalar(item) for item in value) + " ]"
    if isinstance(value, dict):
        return (
            "{ "
            + " , ".join(
                f"{yaml_mapping_key(key)} : {yaml_scalar(item)}"
                for key, item in value.items()
            )
            + " }"
        )
    return yaml_scalar(str(value))


def yaml_mapping_key(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"YAML 映射键必须是字符串，当前值为 {value!r}")
    return yaml_scalar(value)


def flow_map(items: list[tuple[str, Any]], anchor: str | None = None) -> str:
    prefix = f"&{anchor} " if anchor else ""
    return (
        prefix
        + "{ "
        + " , ".join(
            f"{yaml_mapping_key(key)} : {yaml_scalar(value)}" for key, value in items
        )
        + " }"
    )


def ordered_items(proxy: dict[str, Any]) -> list[tuple[str, Any]]:
    order = [
        "name",
        "server",
        "port",
        "type",
        "uuid",
        "flow",
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
    for key in proxy:
        if not isinstance(key, str):
            raise ValueError(f"节点字段名必须是字符串，当前值为 {key!r}")
    items = [(key, proxy[key]) for key in order if key in proxy]
    items.extend(
        (key, proxy[key])
        for key in proxy
        if key not in order and not key.startswith("_")
    )
    return items


def xray_nodes(host_dir: Path, env: dict[str, str], counters: dict[tuple[str, str], int]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    capabilities = host_capabilities(host_dir, env)
    region = capabilities["region"]
    role = capability_role(capabilities)
    private_source = host_dir / "secrets" / "xray-inbounds.json"
    xray_files = (
        [private_source]
        if private_source.exists()
        else sorted((host_dir / "config" / "xray").glob("*.json"))
    )
    for file in xray_files:
        try:
            data = json.loads(file.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"{file}: 无法读取有效的 Xray JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{file}: Xray 配置顶层必须是映射")
        inbounds = data.get("inbounds", []) or []
        if not isinstance(inbounds, list):
            raise ValueError(f"{file}: inbounds 必须是列表")
        for inbound_index, inbound in enumerate(inbounds):
            if not isinstance(inbound, dict):
                raise ValueError(f"{file}: inbounds[{inbound_index}] 必须是映射")
            if inbound.get("protocol") != "vless":
                continue
            settings = inbound.get("settings", {})
            if not isinstance(settings, dict):
                raise ValueError(f"{file}: inbounds[{inbound_index}].settings 必须是映射")
            clients = settings.get("clients")
            if not isinstance(clients, list):
                raise ValueError(
                    f"{file}: inbounds[{inbound_index}].settings.clients 必须是非空列表，提供客户端凭据"
                )
            if not clients:
                raise ValueError(
                    f"{file}: inbounds[{inbound_index}].settings.clients 不能为空，缺少客户端凭据"
                )
            client = clients[0]
            if not isinstance(client, dict):
                raise ValueError(
                    f"{file}: inbounds[{inbound_index}].settings.clients[0] 必须是映射"
                )
            uuid = client.get("id")
            uuid = require_nonempty_string(
                uuid,
                f"{file}: inbounds[{inbound_index}].settings.clients[0].id",
                trim=False,
            )

            field = f'{file}: inbounds[{inbound_index}]'
            config: dict[str, Any] = {
                "type": "vless",
                "server": require_nonempty_string(env.get('VPS_HOST'), f'{field}.VPS_HOST'),
                "port": parse_port(
                    inbound.get("port"),
                    f"{file}: inbounds[{inbound_index}].port",
                ),
                "uuid": uuid,
                **xray_options(inbound, client, field),
            }
            config['_conversion-audit']['derived'].append(
                f'client credential: first accepted entry of {len(clients)}'
            )

            key = (region, "vless")
            idx = counters.get(key, 0)
            counters[key] = idx + 1
            out.append(
                attach_capabilities(
                    {
                        "name": node_name(
                            region,
                            "vless",
                            idx,
                            role,
                            allow_direct_exit=capabilities["allow_direct_exit"],
                            allow_download=capabilities["allow_download"],
                            allow_showip=capabilities["allow_showip"],
                        ),
                        **config,
                    },
                    capabilities,
                )
            )
    return out


def hy2_node(host_dir: Path, env: dict[str, str], counters: dict[tuple[str, str], int]) -> dict[str, Any] | None:
    private_source = host_dir / "secrets" / "hysteria.yaml"
    path = (
        private_source
        if private_source.exists()
        else host_dir / "config" / "hysteria" / "config.yaml"
    )
    if not path.exists():
        return None
    try:
        data = load_yaml(path) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: Hysteria 配置不是有效 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: Hysteria 配置顶层必须是映射")
    options = hysteria_options(data, str(path))
    server = require_nonempty_string(env.get('VPS_HOST'), f'{path}: VPS_HOST')
    certificate_name = cert_domain(str((data.get('tls') or {}).get('cert', '')))
    if 'sni' not in options and certificate_name:
        options['sni'] = certificate_name
        options['_conversion-audit']['derived'].append('sni from tls.cert live certificate directory')
    if 'listen' not in data or data['listen'] in (None, ''):
        raise ValueError(f'{path}: listen 缺失，请在客户端 inventory 中明确端口')
    capabilities = host_capabilities(host_dir, env)
    region = capabilities["region"]
    role = capability_role(capabilities)
    key = (region, "hysteria2")
    idx = counters.get(key, 0)
    counters[key] = idx + 1
    return attach_capabilities({
        "name": node_name(
            region,
            "hysteria2",
            idx,
            role,
            allow_direct_exit=capabilities["allow_direct_exit"],
            allow_download=capabilities["allow_download"],
            allow_showip=capabilities["allow_showip"],
        ),
        "type": "hysteria2",
        "server": server,
        "port": port_from_listen(data['listen'], 443),
        **options,
    }, capabilities)


def client_inventory_nodes(
    host_dir: Path,
    env: dict[str, str],
    counters: dict[tuple[str, str], int],
) -> list[dict[str, Any]] | None:
    """Read an optional client-facing inventory for non-standard/NAT hosts.

    The inventory is authoritative for that host. It describes public client
    addresses and ports, which may differ from the service's internal listen
    ports. Unlike airport imports, these remain self-hosted nodes and can
    participate in proxy chains.
    """
    private_source = host_dir / "secrets" / "client" / "clash-nodes.yaml"
    path = (
        private_source
        if private_source.exists()
        else host_dir / "client" / "clash-nodes.yaml"
    )
    if not path.exists():
        return None
    try:
        data = load_yaml(path)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: 客户端节点 inventory 不是有效 YAML：{exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 顶层必须是映射，并包含 proxies 列表")
    raw_source_nodes = data.get("proxies")
    source_nodes = [] if raw_source_nodes is None else raw_source_nodes
    if not isinstance(source_nodes, list):
        raise ValueError(f"{path}: proxies 必须是列表")

    capabilities = host_capabilities(host_dir, env)
    region = capabilities["region"]
    role = capability_role(capabilities)
    out: list[dict[str, Any]] = []
    for source_index, source in enumerate(source_nodes):
        if not isinstance(source, dict):
            raise ValueError(f"{path}: proxies 条目必须是映射")
        if "dialer-proxy" in source or "<<" in source:
            raise ValueError(
                f"{path}: proxies[{source_index}] 必须是基础节点，"
                "不能包含 dialer-proxy 或 <<"
            )
        protocol = str(source.get("type", "")).lower()
        if protocol not in {"vless", "hysteria2", "socks5"}:
            raise ValueError(f"{path}: 不支持的自建节点协议 {protocol or '<empty>'}")
        server = require_nonempty_string(
            source.get("server"),
            f"{path}: proxies[{source_index}].server",
        )
        port = parse_port(source.get("port"), f"{path}: {protocol}.port")

        key = (region, protocol)
        idx = counters.get(key, 0)
        counters[key] = idx + 1
        proxy = copy.deepcopy(source)
        proxy["server"] = server
        proxy["port"] = port
        proxy["type"] = protocol
        require_proxy_credentials(
            proxy,
            protocol,
            f"{path}: proxies[{source_index}]",
            authenticated_socks5=True,
        )
        proxy["name"] = node_name(
            region,
            protocol,
            idx,
            role,
            allow_direct_exit=capabilities["allow_direct_exit"],
            allow_download=capabilities["allow_download"],
            allow_showip=capabilities["allow_showip"],
        )
        out.append(attach_capabilities(proxy, capabilities))
    return out


def normalize_trusted_nodes(
    source_nodes: list[dict[str, Any]],
    counters: dict[tuple[str, str], int],
    source_path: Path,
) -> list[dict[str, Any]]:
    """Normalize explicitly trusted, client-side nodes.

    These entries are deliberately separate from airport imports.  A trusted
    node may relay, become a chain landing, enter ShowIP, or be classified as
    HomeIP only when the private file says so explicitly.  Trusted entries are
    unmanaged self-hosted/client nodes, so the generator preserves their
    explicit capability declarations but does not infer them.  SOCKS5 entries
    are supported as optional, authenticated client-facing nodes for
    special/NAT hosts.
    """
    allowed_protocols = {"vless", "hysteria2", "socks5"}
    seen: set[tuple[str, str]] = set()
    normalized: list[dict[str, Any]] = []

    for index, source in enumerate(source_nodes):
        field = f"{source_path}: nodes[{index}]"
        if not isinstance(source, dict):
            raise ValueError(f"{field} 必须是映射")

        node_id = require_nonempty_string(source.get("id"), f"{field}.id")

        region_value = source.get("region")
        if region_value is None:
            raise ValueError(f"{field}.region 必须填写实际国家代码，例如 DE 或 NL")
        region = normalize_region(str(region_value))
        if region == "gb":
            region = "uk"
        if region == "xx" or not re.fullmatch(r"[a-z]{2}", region):
            raise ValueError(
                f"{field}.region 必须是两位国家代码（例如 DE、NL），当前值为 {region_value!r}"
            )

        raw_proxy = source.get("proxy")
        if not isinstance(raw_proxy, dict):
            raise ValueError(f"{field}.proxy 必须是 Clash 节点映射")
        proxy = copy.deepcopy(raw_proxy)
        protocol = str(proxy.get("type", "")).strip().lower()
        if protocol not in allowed_protocols:
            raise ValueError(
                f"{field}.proxy.type 只支持 vless、hysteria2 或 socks5，"
                f"当前值为 {protocol or '<empty>'}"
            )
        server = require_nonempty_string(
            proxy.get("server"),
            f"{field}.proxy.server",
        )
        proxy["server"] = server
        if "dialer-proxy" in proxy or "<<" in proxy:
            raise ValueError(f"{field}.proxy 只能是基础节点，不能包含 dialer-proxy 或 <<")
        port = parse_port(proxy.get("port"), f"{field}.proxy.port")
        proxy["type"] = protocol
        proxy["port"] = port
        require_proxy_credentials(
            proxy,
            protocol,
            f"{field}.proxy",
            authenticated_socks5=True,
        )

        identity = (node_id, protocol)
        if identity in seen:
            raise ValueError(f"{field}: id={node_id!r} 的 {protocol} 节点重复")
        seen.add(identity)

        exit_type = str(source.get("exit-type", "general")).strip().lower() or "general"
        if exit_type not in {"general", "homeip"}:
            raise ValueError(
                f"{field}.exit-type 必须是 general 或 homeip，当前值为 {exit_type!r}"
            )
        allow_showip = yaml_bool(source.get("allow-showip"), f"{field}.allow-showip", False)
        allow_relay = yaml_bool(source.get("allow-relay"), f"{field}.allow-relay", False)
        if exit_type == "homeip" and allow_relay:
            raise ValueError(f"{field}.exit-type 为 homeip 时不能设置 allow-relay: true")
        allow_chain_exit = yaml_bool(
            source.get("allow-chain-exit"), f"{field}.allow-chain-exit", False
        )
        allow_direct_exit = yaml_bool(
            source.get("allow-direct-exit"), f"{field}.allow-direct-exit", True
        )
        allow_download = yaml_bool(
            source.get("allow-download"), f"{field}.allow-download", False
        )

        relay_protocol = str(source.get("relay-protocol", protocol)).strip().lower()
        chain_exit_protocol = str(
            source.get("chain-exit-protocol", protocol)
        ).strip().lower()
        if relay_protocol not in allowed_protocols:
            raise ValueError(
                f"{field}.relay-protocol 必须是 vless、hysteria2 或 socks5"
            )
        if chain_exit_protocol not in allowed_protocols:
            raise ValueError(
                f"{field}.chain-exit-protocol 必须是 vless、hysteria2 或 socks5"
            )
        if allow_relay and relay_protocol != protocol:
            raise ValueError(
                f"{field}.relay-protocol 必须与 proxy.type 相同，才能作为 Relay"
            )
        if allow_chain_exit and chain_exit_protocol != protocol:
            raise ValueError(
                f"{field}.chain-exit-protocol 必须与 proxy.type 相同，才能作为 Chain 落地"
            )

        capabilities = {
            "region": region,
            "allow_relay": allow_relay,
            "allow_direct_exit": allow_direct_exit,
            "allow_chain_exit": allow_chain_exit,
            "allow_download": allow_download,
            "allow_showip": allow_showip,
            "exit_type": exit_type,
            "physical_node_id": f"trusted:{node_id}",
            "relay_protocol": relay_protocol,
            "chain_exit_protocol": chain_exit_protocol,
        }
        role = capability_role(capabilities)
        key = (region, protocol)
        node_index = counters.get(key, 0)
        counters[key] = node_index + 1
        proxy["name"] = (
            node_name(
                region,
                protocol,
                node_index,
                role,
                allow_direct_exit=allow_direct_exit,
                allow_download=allow_download,
                allow_showip=allow_showip,
            )
        )
        normalized.append(attach_capabilities(proxy, capabilities))
    return normalized


def load_trusted_nodes(
    path: Path,
    counters: dict[tuple[str, str], int],
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = load_yaml(path)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: trusted-nodes.yaml 不是有效 YAML：{exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 顶层必须是映射，并包含 nodes 列表")
    if "proxies" in data:
        raise ValueError(
            f"{path}: trusted-nodes.yaml 只支持 nodes 列表；"
            "请将节点改为高级 nodes 格式"
        )
    if "nodes" not in data:
        if data:
            raise ValueError(f"{path}: 顶层必须包含 nodes 列表")
        return []
    raw_nodes = data.get("nodes")
    source_nodes = [] if raw_nodes is None else raw_nodes
    if not isinstance(source_nodes, list):
        raise ValueError(f"{path}: nodes 必须是列表")
    return normalize_trusted_nodes(source_nodes, counters, path)


def default_hosts_dir() -> Path:
    """优先使用显式环境变量和标准私有目录，并兼容旧目录结构。"""
    configured = os.environ.get("CLASH_HOSTS_DIR")
    if configured:
        return Path(configured).expanduser()
    for candidate in (
        Path.home() / ".config" / "infra" / "hosts",
        Path.home() / "servers" / "hosts",
        SCRIPT_DIR,
        SCRIPT_DIR.parent,
    ):
        if any(
            path.is_dir()
            and path.name != "vps-template"
            and (path / "host.env").is_file()
            for path in candidate.glob("vps-*")
        ):
            return candidate
    return SCRIPT_DIR


def default_airport_dir(hosts_dir: Path) -> Path:
    """机场订阅默认独立于 hosts 和公开 Git 仓库，并兼容旧位置。"""
    configured = os.environ.get("CLASH_AIRPORT_DIR")
    if configured:
        return Path(configured).expanduser()
    for candidate in (
        Path.home() / ".config" / "clash" / "airport",
        Path.home() / "servers" / "proxy" / "airport",
    ):
        if candidate.exists():
            return candidate
    return hosts_dir / "airport"


def default_trusted_nodes_file(airport_dir: Path) -> Path:
    configured = os.environ.get("CLASH_TRUSTED_NODES_FILE")
    if configured:
        return Path(configured).expanduser()
    return airport_dir / "trusted-nodes.yaml"


def default_ansible_host_vars_dir() -> Path | None:
    configured = os.environ.get("CLASH_ANSIBLE_HOST_VARS_DIR")
    if configured:
        return Path(configured).expanduser()
    candidate = SCRIPT_DIR.parent / "infra" / "ansible" / "host_vars"
    return candidate if candidate.is_dir() else None


def collect_proxies(
    hosts_dir: Path,
    ansible_host_vars_dir: Path | None = None,
    counters: dict[tuple[str, str], int] | None = None,
) -> list[dict[str, Any]]:
    counters = counters if counters is not None else {}
    proxies: list[dict[str, Any]] = []
    # A retired controller source keeps its vps-* directory, secrets, and
    # matching Ansible host_vars for recovery.  Only an active host.env makes
    # that directory part of the client node inventory.
    host_dirs = [
        path
        for path in hosts_dir.glob("vps-*")
        if path.name != "vps-template" and (path / "host.env").is_file()
    ]

    def clash_env(host_dir: Path) -> dict[str, str]:
        env = load_env(host_dir / "host.env")
        if ansible_host_vars_dir:
            env.update(
                load_ansible_clash_vars(
                    ansible_host_vars_dir / f"{host_dir.name}.yml"
                )
            )
        return env

    def host_order(host_dir: Path) -> int:
        env = clash_env(host_dir)
        raw_order = env.get("VPS_CLASH_ORDER", "0").strip() or "0"
        try:
            return int(raw_order)
        except ValueError as exc:
            raise ValueError(
                f"{host_dir}: VPS_CLASH_ORDER 必须是整数，当前值为 {raw_order!r}"
            ) from exc

    host_dirs.sort(
        key=lambda path: (
            host_order(path),
            path.name,
        )
    )
    for host_dir in host_dirs:
        env = clash_env(host_dir)
        if not env:
            continue
        inventory = client_inventory_nodes(host_dir, env, counters)
        if inventory is not None:
            proxies.extend(inventory)
            continue
        proxies.extend(xray_nodes(host_dir, env, counters))
        hy = hy2_node(host_dir, env, counters)
        if hy:
            proxies.append(hy)
    return proxies


def airport_region(name: str) -> str | None:
    meta = node_meta(name)
    if meta and meta.get("region"):
        code = AIRPORT_REGION_ALIASES.get(meta["region"], meta["region"])
        return code if code != "XX" and re.fullmatch(r"[A-Z]{2}", code) else None
    match = re.search(r"\b([A-Za-z]{2})\s*$", name)
    if not match:
        return None
    code = match.group(1).upper()
    code = AIRPORT_REGION_ALIASES.get(code, code)
    return code if code != "XX" else None


def airport_policy_matches(policy_key: str, hostname: str) -> bool:
    key = policy_key.strip().lower()
    host = hostname.rstrip(".").lower()
    if key.startswith(("*.", "+.")):
        suffix = key[2:]
        return host == suffix or host.endswith("." + suffix)
    return key == host


def is_ip_address(value: str) -> bool:
    candidate = value.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def matching_airport_dns_policy(
    subscription: dict[str, Any], selected: list[dict[str, Any]]
) -> dict[str, Any]:
    dns = subscription.get("dns") or {}
    if not isinstance(dns, dict):
        raise ValueError("机场订阅的 dns 必须是映射")
    source_policy = dns.get("nameserver-policy") or {}
    if not isinstance(source_policy, dict):
        raise ValueError("机场订阅的 dns.nameserver-policy 必须是映射")
    hostnames: set[str] = set()
    for proxy in selected:
        raw_server = proxy.get("server")
        if not isinstance(raw_server, str):
            continue
        hostname = raw_server.strip()
        if hostname and not is_ip_address(hostname):
            hostnames.add(hostname)
    return {
        str(key): copy.deepcopy(value)
        for key, value in source_policy.items()
        if any(airport_policy_matches(str(key), hostname) for hostname in hostnames)
    }


def normalize_direct_source_nodes(
    selected: list[dict[str, Any]],
    counters: dict[tuple[str, str], int] | None = None,
    *,
    source_marker: str,
    description_suffix: str,
    physical_source: str,
    source_kind: str,
) -> list[dict[str, Any]]:
    counters = counters if counters is not None else {}
    normalized: list[dict[str, Any]] = []
    for source_index, source in enumerate(selected):
        field = f"{source_kind}节点[{source_index}]"
        if not isinstance(source, dict):
            raise ValueError(f"{field} 必须是映射")
        if "dialer-proxy" in source or "<<" in source:
            raise ValueError(
                f"{field} 必须是独立直连节点，不能包含 dialer-proxy 或 <<"
            )
        original_name = str(source.get("name", "")).strip()
        region = airport_region(original_name)
        raw_protocol = source.get("type")
        protocol_raw = (
            raw_protocol.strip().lower() if isinstance(raw_protocol, str) else ""
        )
        protocol = "H2" if protocol_raw == "hysteria2" else protocol_raw.upper()
        if not region or not protocol:
            print(f"已跳过无法识别的{source_kind}节点：{original_name or '<unnamed>'}")
            continue
        raw_server = source.get("server")
        if not isinstance(raw_server, str) or not raw_server.strip():
            print(f"已跳过缺少 server 的{source_kind}节点：{original_name or '<unnamed>'}")
            continue
        server = raw_server.strip()
        try:
            port = parse_port(source.get("port"), f"{source_kind}节点 {original_name or '<unnamed>'}.port")
        except ValueError as exc:
            print(f"已跳过无效{source_kind}节点：{exc}")
            continue
        description = f"{REGION_CN.get(region.lower(), region)}{description_suffix}"
        source_label = source_marker_label(original_name)
        proxy = copy.deepcopy(source)
        proxy["server"] = server
        proxy["port"] = port
        proxy["type"] = protocol_raw
        try:
            require_proxy_credentials(proxy, protocol_raw, field)
        except ValueError as exc:
            print(f"已跳过无效{source_kind}节点：{exc}")
            continue
        key = (region.lower(), protocol_raw)
        index = counters.get(key, 0)
        counters[key] = index + 1
        proxy["name"] = (
            f"VPS-[{region}.Exit]-{protocol}-{index:02d}-({description})"
            f"-[{source_marker}={source_label}]"
        )
        # Direct-source nodes remain independently selectable but never
        # participate in generated dialer-proxy chains.
        proxy["_allow-relay"] = False
        proxy["_allow-direct-exit"] = True
        proxy["_allow-chain-exit"] = False
        proxy["_allow-download"] = False
        proxy["_exit-type"] = "general"
        proxy["_physical-node-id"] = f"{physical_source}:{original_name}"
        normalized.append(proxy)
    return normalized


def normalize_airport_nodes(
    selected: list[dict[str, Any]],
    counters: dict[tuple[str, str], int] | None = None,
) -> list[dict[str, Any]]:
    return normalize_direct_source_nodes(
        selected,
        counters,
        source_marker="Airport",
        description_suffix="机场出口",
        physical_source="airport",
        source_kind="机场",
    )


def interactive_airport_import(
    airport_dir: Path,
    counters: dict[tuple[str, str], int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    subscription_path = airport_dir / "subscription.yaml"
    if not subscription_path.exists():
        return [], {}

    answer = input(
        f"是否导入机场订阅？已发现 {subscription_path} [y/N]："
    ).strip().lower()
    if answer not in {"y", "yes", "1", "是"}:
        return [], {}

    try:
        subscription = load_yaml(subscription_path) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"无法读取机场订阅：{exc}")
        return [], {}
    if not isinstance(subscription, dict):
        print("机场订阅顶层必须是映射。")
        return [], {}
    source_list = subscription.get("proxies", []) or []
    if not isinstance(source_list, list):
        print("机场订阅中的 proxies 必须是列表。")
        return [], {}
    source_nodes = [
        proxy
        for proxy in source_list
        if isinstance(proxy, dict) and proxy.get("name") and proxy.get("server")
    ]
    if not source_nodes:
        print("机场订阅中没有可用的 proxies。")
        return [], {}
    print(f"机场订阅包含 {len(source_nodes)} 个节点。")

    selection_path = airport_dir / "selected-nodes.yaml"
    selected: list[dict[str, Any]] = []
    if selection_path.exists():
        try:
            saved = load_yaml(selection_path) or {}
        except (OSError, yaml.YAMLError) as exc:
            print(f"警告：无法读取已保存的机场选择：{exc}")
            saved_names = []
        else:
            if not isinstance(saved, dict):
                print("警告：已保存的机场选择顶层必须是映射。")
                saved_names = []
            else:
                raw_names = saved.get("selected-names", []) or []
                if not isinstance(raw_names, list):
                    print("警告：已保存的机场选择 selected-names 必须是列表。")
                    saved_names = []
                else:
                    saved_names = [str(name) for name in raw_names]
        if saved_names:
            use_saved = input(
                f"已保存 {len(saved_names)} 个机场节点，是否继续使用？ [Y/n]："
            ).strip().lower()
            if use_saved not in {"n", "no", "0", "否"}:
                by_name = {str(proxy["name"]): proxy for proxy in source_nodes}
                selected = [by_name[name] for name in saved_names if name in by_name]
                missing = [name for name in saved_names if name not in by_name]
                if missing:
                    print(f"警告：{len(missing)} 个已保存节点在当前订阅中已不存在。")

    if not selected:
        counts: dict[str, int] = {}
        for proxy in source_nodes:
            region = airport_region(str(proxy["name"]))
            if region:
                counts[region] = counts.get(region, 0) + 1
        priority_regions = [code for code in AIRPORT_REGION_PRIORITY if code in counts]
        other_regions = sorted(code for code in counts if code not in priority_regions)
        regions = priority_regions + other_regions
        print("\n机场节点地区：")
        for region in priority_regions:
            print(f"  {region}（{counts[region]} 个）")
        if other_regions:
            print(f"  另有 {len(other_regions)} 个其他地区代码；输入 ? 查看")
        while True:
            raw_regions = input(
                "请输入地区代码（如 HK,JP,US；?=查看全部；all=全部；0=取消）："
            ).strip().upper()
            if raw_regions in {"?", "HELP", "LIST", "查看"}:
                print("其他地区代码：")
                for start in range(0, len(other_regions), 12):
                    chunk = other_regions[start : start + 12]
                    print("  " + "  ".join(f"{code}({counts[code]})" for code in chunk))
                continue
            if raw_regions in {"0", "N", "NONE", "取消"}:
                return [], {}
            requested = set(regions) if raw_regions in {"ALL", "A", "全部"} else {
                AIRPORT_REGION_ALIASES.get(token.strip(), token.strip())
                for token in raw_regions.split(",")
                if token.strip()
            }
            invalid = sorted(requested - set(regions))
            if requested and not invalid:
                break
            print("地区输入无效" + (f"：{', '.join(invalid)}" if invalid else "。"))
        candidates = [
            proxy for proxy in source_nodes if airport_region(str(proxy["name"])) in requested
        ]
        print(f"\n候选机场节点（{len(candidates)} 个）：")
        for index, proxy in enumerate(candidates, 1):
            print(f"  {index:>3}. {proxy['name']}")
        chosen = prompt_number_selection(len(candidates))
        selected = [proxy for index, proxy in enumerate(candidates) if index in chosen]
        if not selected:
            print("未选择机场节点。")
            return [], {}
        save = input(f"是否保存这 {len(selected)} 个节点的选择？ [Y/n]：").strip().lower()
        if save not in {"n", "no", "0", "否"}:
            secure_write(
                selection_path,
                yaml.safe_dump(
                    {
                        "selected-names": [proxy["name"] for proxy in selected],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
            )
            print(f"已保存选择到 {selection_path}。")

    dns_policy = matching_airport_dns_policy(subscription, selected)
    normalized = normalize_airport_nodes(selected, counters)
    print(
        f"已导入 {len(normalized)} 个机场独立节点（不参与代理链），"
        f"匹配 {len(dns_policy)} 条节点专用 DNS 策略（仅报告数量，不打印或导出内容）；"
        "请在私有订阅的 dns.nameserver-policy 中核对，按需手动合入 home.yaml。"
    )
    return normalized, dns_policy


def secure_write(output: Path, content: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, output)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def clean_proxy(proxy: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in proxy.items():
        if not isinstance(key, str):
            raise ValueError(f"节点字段名必须是字符串，当前值为 {key!r}")
        if not key.startswith("_"):
            cleaned[key] = copy.deepcopy(value)
    return cleaned


def write_plain(proxies: list[dict[str, Any]], output: Path) -> None:
    secure_write(
        output,
        yaml.safe_dump(
            {"proxies": [clean_proxy(proxy) for proxy in proxies]},
            allow_unicode=True,
            sort_keys=False,
        ),
    )


def loon_quote(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        raise ValueError('Loon 字符串参数不能为 null 或容器')
    text = str(value)
    if '\r' in text or '\n' in text:
        raise ValueError('Loon 字符串参数不能含换行')
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def loon_atom(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None or isinstance(value, (dict, list, tuple, set)):
        raise ValueError('Loon 参数不能为 null 或容器')
    text = str(value)
    if '\r' in text or '\n' in text:
        raise ValueError('Loon 参数不能含换行')
    if any(char in text for char in ',="\\') or text != text.strip():
        return loon_quote(text)
    return text


def loon_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        raise ValueError('Loon 布尔参数不能为 null')
    return yaml_bool(value, "Loon 节点布尔参数", default)


def loon_option(key: str, value: Any) -> str:
    if isinstance(value, list):
        if any(not isinstance(item, str) or '\r' in item or '\n' in item for item in value):
            raise ValueError('Loon 列表参数必须是无换行的字符串')
        value = ",".join(str(item) for item in value)
    return f"{key}={loon_atom(value)}"


def loon_server_fields(proxy: dict[str, Any]) -> list[str]:
    raw_server = proxy.get("server")
    if isinstance(raw_server, (dict, list, set, tuple)):
        raise ValueError("缺少 server 或 port")
    server = str(raw_server or "").strip()
    if not server:
        raise ValueError("缺少 server 或 port")
    port = parse_port(proxy.get("port"), "Loon 节点 port")
    return [loon_atom(server), loon_atom(port)]


def loon_transport_options(proxy: dict[str, Any]) -> list[str]:
    network = str(proxy.get("network", "tcp")).strip().lower() or "tcp"
    network = {"raw": "tcp", "websocket": "ws"}.get(network, network)
    if network not in {"tcp", "ws", "http"}:
        raise ValueError(f"Loon 不支持传输方式 {network!r}")

    options = [loon_option("transport", network)] if 'network' in proxy else []
    if network not in {"ws", "http"}:
        return options

    transport_opts = proxy.get(f"{network}-opts", {})
    if not isinstance(transport_opts, dict):
        raise ValueError(f"{network}-opts 必须是映射")
    unknown = set(transport_opts) - {'path', 'headers'}
    if unknown:
        raise ValueError(f'Loon 无法保留 {network}-opts 字段：{", ".join(sorted(unknown))}')
    if 'path' in transport_opts:
        path = transport_opts['path']
        if isinstance(path, list):
            if len(path) != 1:
                raise ValueError('Loon 无法保留多个或空的 path 列表')
            path = path[0]
        if not isinstance(path, str):
            raise ValueError('Loon path 必须是字符串')
        options.append(loon_option('path', path))
    headers = transport_opts.get("headers", {})
    if not isinstance(headers, dict):
        raise ValueError(f"{network}-opts.headers 必须是映射")
    if any(not isinstance(k, str) or k.lower() != 'host' for k in headers) or len(headers) > 1:
        raise ValueError('Loon 无法保留非 Host 或重复的传输 headers')
    if headers:
        host = next(iter(headers.values()))
        if isinstance(host, list):
            if len(host) != 1:
                raise ValueError('Loon 无法保留多个或空的 Host 列表')
            host = host[0]
        if not isinstance(host, str):
            raise ValueError('Loon Host 必须是字符串')
        options.append(loon_option('host', host))
    return options


def loon_vless(proxy: dict[str, Any]) -> str:
    uuid = require_nonempty_string(proxy.get('uuid'), 'VLESS uuid', trim=False)
    fields = ["VLESS", *loon_server_fields(proxy), loon_quote(uuid)]
    options = loon_transport_options(proxy)

    reality = proxy.get("reality-opts") or {}
    if reality and not isinstance(reality, dict):
        raise ValueError("reality-opts 必须是映射")
    public_key = reality.get("public-key") if isinstance(reality, dict) else None
    if public_key:
        options.extend(
            [
                f"public-key={loon_quote(public_key)}",
            ]
        )
        if "short-id" in reality:
            options.append(loon_option("short-id", reality["short-id"]))

    if 'flow' in proxy:
        options.append(loon_option('flow', proxy['flow']))
    for key, output_key in [('udp', 'udp'), ('tls', 'over-tls')]:
        if key in proxy:
            options.append(loon_option(output_key, loon_bool(proxy[key])))
    sni = proxy.get('sni', proxy.get('servername'))
    if sni is not None:
        options.append(loon_option("sni", sni))
    if 'skip-cert-verify' in proxy:
        options.append(loon_option('skip-cert-verify', loon_bool(proxy['skip-cert-verify'])))
    if 'alpn' in proxy:
        options.append(loon_option("alpn", proxy["alpn"]))
    return ",".join([*fields, *options])


def loon_hysteria2(proxy: dict[str, Any]) -> str:
    password = require_nonempty_string(proxy.get('password'), 'Hysteria2 password', trim=False)
    fields = ["Hysteria2", *loon_server_fields(proxy), loon_quote(password)]
    options: list[str] = []
    sni = proxy.get('sni', proxy.get('servername'))
    if sni is not None:
        options.append(loon_option("sni", sni))
    if 'skip-cert-verify' in proxy:
        options.append(loon_option('skip-cert-verify', loon_bool(proxy['skip-cert-verify'])))
    if "fast-open" in proxy:
        options.append(loon_option("fast-open", loon_bool(proxy["fast-open"])))
    if 'salamander-password' in proxy:
        options.append(
            f"salamander-password={loon_quote(proxy['salamander-password'])}"
        )
    elif 'obfs-password' in proxy:
        options.append(
            f"salamander-password={loon_quote(proxy['obfs-password'])}"
        )
    if "udp" in proxy:
        options.append(loon_option("udp", loon_bool(proxy["udp"])))
    return ",".join([*fields, *options])


def loon_shadowsocks(proxy: dict[str, Any]) -> str:
    raw_cipher = proxy.get("cipher")
    cipher = (
        ""
        if isinstance(raw_cipher, (dict, list, set, tuple))
        else str(raw_cipher or "").strip()
    )
    password = proxy.get("password")
    if not cipher or password is None or password == "" or isinstance(
        password, (dict, list, set, tuple)
    ):
        raise ValueError("Shadowsocks 缺少 cipher 或 password")
    fields = [
        "Shadowsocks",
        *loon_server_fields(proxy),
        loon_atom(cipher),
        loon_quote(password),
    ]
    options: list[str] = []
    plugin = str(proxy.get("plugin", "")).strip().lower()
    if plugin:
        if plugin not in {"obfs", "simple-obfs"}:
            raise ValueError(f"Loon 不支持 Shadowsocks 插件 {plugin!r}")
        plugin_opts = proxy.get("plugin-opts") or {}
        if not isinstance(plugin_opts, dict):
            raise ValueError("plugin-opts 必须是映射")
        if "mode" in plugin_opts:
            options.append(loon_option("obfs-name", plugin_opts["mode"]))
        if "host" in plugin_opts:
            options.append(loon_option("obfs-host", plugin_opts["host"]))
        if "path" in plugin_opts:
            options.append(loon_option("obfs-uri", plugin_opts["path"]))
    if "udp" in proxy:
        options.append(loon_option("udp", loon_bool(proxy["udp"])))
    return ",".join([*fields, *options])


def loon_socks5(proxy: dict[str, Any]) -> str:
    require_socks5_credentials(proxy, "SOCKS5")
    fields = [
        "socks5",
        *loon_server_fields(proxy),
        loon_atom(proxy["username"]),
        loon_quote(proxy["password"]),
    ]
    options: list[str] = []
    if "tls" in proxy:
        options.append(loon_option("over-tls", loon_bool(proxy["tls"])))
    sni = proxy.get("sni", proxy.get("servername"))
    if sni is not None:
        options.append(loon_option("sni", sni))
    if "skip-cert-verify" in proxy:
        options.append(loon_option("skip-cert-verify", loon_bool(proxy["skip-cert-verify"])))
    if "udp" in proxy:
        options.append(loon_option("udp", loon_bool(proxy["udp"])))
    return ",".join([*fields, *options])


def loon_node_body(proxy: dict[str, Any]) -> str:
    protocol = str(proxy.get("type", "")).strip().lower()
    converters = {
        "vless": loon_vless,
        "hysteria2": loon_hysteria2,
        "ss": loon_shadowsocks,
        "socks5": loon_socks5,
    }
    converter = converters.get(protocol)
    if converter is None:
        raise ValueError(f"Loon 不支持节点协议 {protocol or '<empty>'}")
    common = {'name', 'type', 'server', 'port', 'udp'}
    supported = {
        'vless': {'uuid', 'flow', 'tls', 'network', 'ws-opts', 'http-opts',
                  'reality-opts', 'sni', 'servername', 'skip-cert-verify', 'alpn', 'encryption'},
        'hysteria2': {'password', 'sni', 'servername', 'skip-cert-verify',
                      'fast-open', 'salamander-password', 'obfs', 'obfs-password'},
        'ss': {'cipher', 'password', 'plugin', 'plugin-opts'},
        'socks5': {'username', 'password', 'tls', 'sni', 'servername', 'skip-cert-verify'},
    }
    unknown = set(clean_proxy(proxy)) - common - supported[protocol]
    if unknown:
        raise ValueError(f'Loon 无法保留字段：{", ".join(sorted(unknown))}')
    if 'sni' in proxy and 'servername' in proxy and proxy['sni'] != proxy['servername']:
        raise ValueError('Loon sni 与 servername 冲突')
    for key in ('sni', 'servername'):
        if key in proxy and not isinstance(proxy[key], str):
            raise ValueError(f'Loon {key} 必须是字符串')
    if protocol == 'vless':
        if 'flow' in proxy and (not isinstance(proxy['flow'], str)
                                or '\n' in proxy['flow'] or '\r' in proxy['flow']):
            raise ValueError('Loon flow 必须是有效字符串')
        if 'alpn' in proxy and (not isinstance(proxy['alpn'], list)
                                or any(not isinstance(v, str) for v in proxy['alpn'])):
            raise ValueError('Loon alpn 必须是字符串列表')
        if proxy.get('encryption', 'none') not in ('', 'none'):
            raise ValueError('Loon 无法保留 VLESS encryption')
        reality = proxy.get('reality-opts', {})
        if not isinstance(reality, dict) or set(reality) - {'public-key', 'short-id'}:
            raise ValueError('Loon 无法保留 reality-opts 字段')
        if reality and not reality.get('public-key'):
            raise ValueError('Loon REALITY 缺少 public-key')
        if 'public-key' in reality:
            require_nonempty_string(reality['public-key'], 'Loon REALITY public-key', trim=False)
        if 'short-id' in reality and not isinstance(reality['short-id'], str):
            raise ValueError('Loon REALITY short-id 必须是字符串')
        network = proxy.get('network', 'tcp')
        if not isinstance(network, str):
            raise ValueError('Loon network 必须是字符串')
        network = network.strip().lower() or 'tcp'
        network = {'raw': 'tcp', 'websocket': 'ws'}.get(network, network)
        for key in {'ws-opts', 'http-opts'} & set(proxy):
            if key != f'{network}-opts':
                raise ValueError(f'Loon {key} 与 network 不一致')
    if protocol == 'hysteria2':
        if 'obfs' in proxy and proxy['obfs'] != 'salamander':
            raise ValueError('Loon 不支持该 obfs 类型')
        if 'obfs-password' in proxy and proxy.get('obfs') != 'salamander':
            raise ValueError('Loon obfs-password 缺少明确的 salamander 类型')
        if proxy.get('obfs') == 'salamander':
            require_nonempty_string(proxy.get('obfs-password'), 'Loon obfs-password', trim=False)
        if 'salamander-password' in proxy:
            require_nonempty_string(proxy['salamander-password'], 'Loon salamander-password', trim=False)
            if 'obfs-password' in proxy and proxy['obfs-password'] != proxy['salamander-password']:
                raise ValueError('Loon 混淆密码字段冲突')
    if protocol == 'ss':
        require_nonempty_string(proxy.get('password'), 'Shadowsocks password', trim=False)
        opts = proxy.get('plugin-opts', {})
        if not isinstance(opts, dict) or set(opts) - {'mode', 'host', 'path'}:
            raise ValueError('Loon 无法保留 plugin-opts 字段')
        if opts and not proxy.get('plugin'):
            raise ValueError('Loon plugin-opts 缺少 plugin')
        if any(not isinstance(value, str) for value in opts.values()):
            raise ValueError('Loon plugin-opts 参数必须是字符串')
    if protocol == 'socks5':
        require_socks5_credentials(proxy, 'SOCKS5')
    return converter(proxy)


def loon_node_alias(proxy: dict[str, Any], counts: dict[str, int]) -> str:
    name = str(proxy.get("name", ""))
    meta = node_meta(name)
    region = (meta.get("region") if meta else None) or "XX"
    protocol = str(proxy.get("type", "node")).strip().lower()
    protocol = {"hysteria2": "hy2"}.get(protocol, protocol or "node")
    attribute = ""
    if meta and meta["role"] == "HomeIP":
        attribute = ".homeip"
    elif (meta and meta["role"] == "Exit"
          and proxy.get("_allow-chain-exit") is not False
          and (proxy.get("_allow-direct-exit") is False
               or meta["direct"] == "false")):
        attribute = ".landing"
    base = f"{region.lower()}{attribute}.{protocol}"
    index = counts.get(base, 0)
    counts[base] = index + 1
    return base if index == 0 else f"{base}-{index:02d}"


def loon_proxy_groups(
    aliases: dict[str, str],
    chain_aliases: dict[str, str],
    home_template: Path = HOME_TEMPLATE,
) -> list[str]:
    """Render the four home group layers using only exported Loon members."""
    source = load_home_proxy_groups(home_template)
    layers = ("DirectExit", "Chain", "Line", "Route")
    selected = {
        name: group for name, group in source.items()
        if any(f".{layer}-[" in name for layer in layers)
    }
    members: dict[str, list[str]] = {}

    def available(name: str) -> bool:
        if name in members:
            return bool(members[name])
        group = selected[name]
        if ".DirectExit-[" in name and "filter" in group:
            candidates = [alias for original, alias in aliases.items()
                          if _group_matches_proxy(group, original)]
        elif ".Chain-[" in name and "filter" in group:
            candidates = [alias for original, alias in chain_aliases.items()
                          if _group_matches_proxy(group, original)]
        else:
            candidates = [candidate for candidate in group["proxies"]
                          if candidate in {"DIRECT", "REJECT"}
                          or (candidate in selected and available(candidate))]
        # The home graph is already checked for cycles by load_home_proxy_groups.
        members[name] = list(dict.fromkeys(candidates))
        return bool(members[name])

    for name in selected:
        available(name)

    lines: list[str] = []
    for layer in layers:
        for name, group in selected.items():
            if f".{layer}-[" not in name or not members.get(name):
                continue
            kind = group["type"]
            if kind not in {"select", "url-test", "fallback"}:
                raise ValueError(f"Loon 不支持策略组类型 {kind}: {name}")
            options = [kind, *members[name]]
            if kind != "select":
                if "url" in group:
                    options.append(f"url = {group['url']}")
                if "interval" in group:
                    options.append(f"interval = {group['interval']}")
                if kind == "url-test" and "tolerance" in group:
                    options.append(f"tolerance = {group['tolerance']}")
                if kind == "fallback" and "timeout" in group:
                    options.append(f"max-timeout = {group['timeout']}")
            lines.append(f"{name} = {', '.join(map(str, options))}")
    return lines


def write_loon(
    proxies: list[dict[str, Any]],
    output: Path,
    chains: list[tuple[dict[str, Any], dict[str, Any]]] = (),
    home_template: Path = HOME_TEMPLATE,
) -> tuple[int, int, list[str]]:
    """Write Loon nodes and the selected, representable proxy chains."""
    node_lines: list[str] = []
    chain_lines: list[str] = []
    counts: dict[str, int] = {}
    skipped: list[str] = []
    aliases: dict[str, str] = {}
    chain_aliases: dict[str, str] = {}
    valid_pairs = {
        (exit_proxy["name"], dialer["name"])
        for exit_proxy, dialer in chain_candidates(proxies)
    }
    for proxy in proxies:
        try:
            body = loon_node_body(proxy)
        except ValueError as exc:
            skipped.append(f"{proxy.get('name', '<unnamed>')}: {exc}")
            continue
        alias = loon_node_alias(proxy, counts)
        aliases[proxy["name"]] = alias
        node_lines.append(f"{alias} = {body}")

    emitted_pairs: set[tuple[str, str]] = set()
    for exit_proxy, dialer in chains:
        pair = (exit_proxy["name"], dialer["name"])
        if pair in emitted_pairs:
            continue
        if pair not in valid_pairs:
            skipped.append(f"代理链 {pair[0]} <- {pair[1]}: 不符合组链条件")
            continue
        if str(dialer.get("type", "")).lower() != "vless":
            skipped.append(f"代理链 {pair[0]} <- {pair[1]}: Loon 入口仅支持 VLESS")
            continue
        if pair[0] not in aliases or pair[1] not in aliases:
            skipped.append(f"代理链 {pair[0]} <- {pair[1]}: 出入口有节点未导出到 Loon")
            continue
        exit_alias, entry_alias = aliases[pair[0]], aliases[pair[1]]
        alias = f"chain.{exit_alias}.via.{entry_alias}"
        chain_lines.append(f"{alias} = {entry_alias}, {exit_alias}")
        chain_aliases[chain_name(exit_proxy, dialer)] = alias
        emitted_pairs.add(pair)

    lines = ["[Proxy]", *node_lines]
    if chain_lines:
        lines.extend(["", "[Proxy Chain]", *chain_lines])
    group_lines = loon_proxy_groups(aliases, chain_aliases, home_template)
    if group_lines:
        lines.extend(["", "[Proxy Group]", *group_lines])
    secure_write(output, "\n".join(lines) + "\n")
    return len(node_lines), len(chain_lines), skipped


def chain_name(exit_proxy: dict[str, Any], dialer: dict[str, Any]) -> str:
    exit_meta = node_meta(exit_proxy["name"])
    dialer_meta = node_meta(dialer["name"])
    exit_tag = exit_meta["region"]
    if exit_meta["role"] == "HomeIP":
        exit_tag += f".{exit_meta['role']}"
    name = (
        f"PrxChain-[{exit_tag}]-{exit_meta['proto']}-{exit_meta['idx']}"
        f"--<<-{dialer_meta['region']}.{dialer_meta['role']}.{dialer_meta['proto']}.{dialer_meta['idx']}"
        f"-(代理链=={exit_meta['desc']}<-{dialer_meta['desc']})"
    )
    if exit_proxy.get("_allow-showip", False):
        name += "-[ShowIP=true]"
    return name


def proxy_source_directory(proxy: dict[str, Any]) -> str | None:
    source = str(proxy.get("_physical-node-id", "")).strip()
    return source if source.startswith("vps-") else None


def interactive_proxy_source(proxy: dict[str, Any], prefix: str = "") -> str | None:
    source = proxy_source_directory(proxy)
    if source:
        return f"{prefix}目录: {source}" if prefix else f"来源目录: {source}"

    physical_node_id = str(proxy.get("_physical-node-id", "")).strip()
    if physical_node_id.startswith("trusted:"):
        return f"{prefix}来源文件: trusted-nodes.yaml"
    return None


def interactive_proxy_label(proxy: dict[str, Any]) -> str:
    name = str(proxy.get("name", "<unnamed>"))
    source_label = interactive_proxy_source(proxy)
    if not source_label:
        return name
    return f"{name}（{source_label}）"


def interactive_chain_label(
    candidate: tuple[dict[str, Any], dict[str, Any]],
) -> str:
    exit_proxy, dialer = candidate
    name = chain_name(exit_proxy, dialer)
    exit_source = interactive_proxy_source(exit_proxy, "出口")
    dialer_source = interactive_proxy_source(dialer, "入口")
    labels: list[str] = []
    if exit_source:
        labels.append(exit_source)
    if dialer_source:
        labels.append(dialer_source)
    return f"{name}（{'；'.join(labels)}）" if labels else name


def chain_candidates(proxies: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for exit_proxy in proxies:
        exit_meta = node_meta(exit_proxy["name"])
        if not exit_meta:
            continue
        if not exit_proxy.get("_allow-chain-exit", True):
            continue
        if exit_proxy.get("_chain-exit-protocol") not in {None, str(exit_proxy.get("type", "")).lower()}:
            continue
        for dialer in proxies:
            dialer_meta = node_meta(dialer["name"])
            if (
                not dialer_meta
                or not dialer.get("_allow-relay", dialer_meta["role"] in {"Core", "Relay"})
                or dialer.get("_relay-protocol") not in {None, str(dialer.get("type", "")).lower()}
            ):
                continue
            exit_identity = exit_proxy.get("_physical-node-id")
            dialer_identity = dialer.get("_physical-node-id")
            if exit_identity and dialer_identity and exit_identity == dialer_identity:
                continue
            candidates.append((exit_proxy, dialer))
    return candidates


def route_key(candidate: tuple[dict[str, Any], dict[str, Any]]) -> tuple[str, str]:
    exit_proxy, dialer = candidate
    exit_meta = node_meta(exit_proxy["name"])
    exit_tag = exit_meta["region"]
    if exit_meta["role"] == "HomeIP":
        exit_tag += f".{exit_meta['role']}"
    return normalize_route_region(exit_tag), normalize_route_region(node_meta(dialer["name"])["region"])


def normalize_route_region(value: str) -> str:
    text = value.strip().upper()
    if text.endswith(".HOMEIP"):
        region = text[: -len(".HOMEIP")]
        if not re.fullmatch(r"[A-Z]{2}", region):
            raise ValueError(f"无效地区代码 {value!r}")
        return f"{region}.HomeIP"
    if not re.fullmatch(r"[A-Z]{2}", text):
        raise ValueError(f"无效地区代码 {value!r}")
    return text


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
        except ValueError as exc:
            print(f"输入无效：{exc}")
        except EOFError as exc:
            raise SystemExit("输入已结束，已取消选择") from exc


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
        print(f"  {index:>2}. {interactive_proxy_label(proxy)}")
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


def interactive_selection(
    proxies: list[dict[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    str,
    list[tuple[dict[str, Any], dict[str, Any]]],
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
        return proxies, output_format, []

    candidates = chain_candidates(proxies)

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
        return proxies, output_format, candidates
    if choice == "4":
        return proxies, output_format, []
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
        )

    print("\n具体代理链：")
    for index, candidate in enumerate(candidates, 1):
        print(f"  {index:>2}. {interactive_chain_label(candidate)}")
    selected = prompt_number_selection(len(candidates))
    return (
        proxies,
        output_format,
        [candidate for index, candidate in enumerate(candidates) if index in selected],
    )


def select_routes(
    proxies: list[dict[str, Any]], route_spec: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    candidates = chain_candidates(proxies)
    available = {route_key(candidate) for candidate in candidates}
    requested: set[tuple[str, str]] = set()
    for raw in route_spec.split(","):
        token = raw.strip().replace(" ", "")
        if not token:
            continue
        separator = "<-" if "<-" in token else ":"
        if separator not in token:
            raise SystemExit(f"无效方向 {raw!r}；请使用 'HK<-JP,US<-HK' 或 'HK:JP,US:HK'")
        raw_exit_region, raw_dialer_region = token.split(separator, 1)
        try:
            exit_region = normalize_route_region(raw_exit_region)
            dialer_region = normalize_route_region(raw_dialer_region)
        except ValueError as exc:
            raise SystemExit(f"无效方向 {raw!r}：{exc}") from exc
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
    ensure_unique_anchors(proxies)
    lines: list[str] = ["proxies:"]
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
        "--home-template",
        type=Path,
        default=HOME_TEMPLATE,
        help="用于校验代理组筛选的 home.yaml（默认仓库中的 home.yaml）",
    )
    parser.add_argument(
        "--hosts-dir",
        type=Path,
        default=default_hosts_dir(),
        help="包含 vps-* 私有配置目录（默认 CLASH_HOSTS_DIR 或 ~/.config/infra/hosts）",
    )
    parser.add_argument(
        "--airport-dir",
        type=Path,
        help="机场私有订阅目录（默认 CLASH_AIRPORT_DIR 或 ~/.config/clash/airport）",
    )
    parser.add_argument(
        "--trusted-nodes-file",
        type=Path,
        help=(
            "私有可信节点 YAML（默认 CLASH_TRUSTED_NODES_FILE 或 "
            "<airport-dir>/trusted-nodes.yaml）"
        ),
    )
    parser.add_argument(
        "--ansible-host-vars-dir",
        type=Path,
        default=default_ansible_host_vars_dir(),
        help=(
            "Ansible host_vars 目录；其中 vps_clash_* 公共角色字段优先于 "
            "host.env（默认 CLASH_ANSIBLE_HOST_VARS_DIR 或相邻 infra 仓库）"
        ),
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="输出文件（plain 默认 nodes.yaml，其余格式默认 clash-vps.generated.yaml）",
    )
    parser.add_argument("--raw-output", type=Path, help="额外输出仅含基础节点的 proxies YAML")
    parser.add_argument('--audit', action='store_true', help='显示参数转换审计（仅字段名，不显示凭据值）')
    parser.add_argument(
        "--loon-output",
        type=Path,
        default=LOON_OUT,
        help="额外输出 Loon 节点文件（默认 loon-nodes.conf）",
    )
    parser.add_argument(
        "--no-loon",
        dest="loon_output",
        action="store_const",
        const=None,
        help="不生成 Loon 节点文件",
    )
    formats = parser.add_mutually_exclusive_group()
    formats.add_argument("--plain", action="store_true", help="仅输出基础节点")
    formats.add_argument(
        "--template",
        action="store_true",
        help="输出 proxies 模板和代理链（与 --merge 同义）",
    )
    formats.add_argument(
        "--merge",
        action="store_true",
        help="输出 Clash Verge Rev 扩展配置（与 --template 同义）",
    )
    parser.add_argument("--interactive", "-i", action="store_true", help="交互选择输出格式和代理链")
    parser.add_argument(
        "--chains",
        choices=("all", "none"),
        default=None,
        help="非交互模式生成全部或不生成代理链；指定后默认使用模板格式",
    )
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
    args = parser.parse_args(argv)
    if args.interactive and any(
        (
            args.plain,
            args.template,
            args.merge,
            args.chains is not None,
            args.routes is not None,
            bool(args.exclude_node),
        )
    ):
        parser.error(
            "--interactive 不能与 --plain/--template/--merge/--chains/"
            "--routes/--exclude-node 同时使用"
        )
    if args.plain and (args.chains is not None or args.routes is not None):
        parser.error("--plain 不能与 --chains 或 --routes 同时使用")
    if args.routes is not None and args.chains is not None:
        parser.error("--routes 不能与 --chains 同时使用")
    return args


def apply_default_invocation(args: argparse.Namespace, invoked_without_args: bool) -> None:
    if not invoked_without_args:
        return
    args.interactive = True
    args.output = SCRIPT_DIR / "clash-vps.generated.yaml"
    args.raw_output = SCRIPT_DIR / "nodes.yaml"


def validate_output_paths(
    paths: list[tuple[str, Path | None]],
    inputs: list[Path] | None = None,
) -> None:
    seen: dict[Path, str] = {}
    inodes: dict[tuple[int, int], str] = {}
    protected = {path.expanduser().resolve() for path in inputs or []}
    protected_inodes = {
        (p.stat().st_dev, p.stat().st_ino) for p in protected if p.is_file()
    }
    for label, path in paths:
        if path is None:
            continue
        resolved = path.expanduser().resolve()
        if resolved.exists() and not resolved.is_file():
            raise SystemExit(f"输出路径无效：{label} 必须是普通文件 {path}")
        for parent in resolved.parents:
            if parent.exists() and not parent.is_dir():
                raise SystemExit(f"输出路径无效：{label} 的父路径不是目录 {parent}")
        for previous_path, previous_label in seen.items():
            if previous_path in resolved.parents or resolved in previous_path.parents:
                raise SystemExit(f"输出路径冲突：{previous_label} 和 {label} 不能互为父路径")
        identity = None
        if resolved.is_file():
            status = resolved.stat()
            identity = (status.st_dev, status.st_ino)
        if resolved in protected or identity in protected_inodes:
            raise SystemExit(f"输出路径冲突：{label} 会覆盖输入文件 {path}")
        previous = seen.get(resolved)
        if identity is not None:
            previous = previous or inodes.get(identity)
            inodes[identity] = label
        if previous is not None:
            raise SystemExit(
                f"输出路径冲突：{previous} 和 {label} 都是 {resolved}；"
                "请显式指定不同文件"
            )
        seen[resolved] = label


def input_paths(hosts_dir: Path, airport_dir: Path, trusted_file: Path,
                ansible_dir: Path | None) -> list[Path]:
    paths = [trusted_file, airport_dir / 'subscription.yaml',
             airport_dir / 'selected-nodes.yaml']
    for host in hosts_dir.glob('vps-*'):
        if host.name == 'vps-template' or not (host / 'host.env').is_file():
            continue
        paths.extend(host / relative for relative in (
            'host.env', 'secrets/client/clash-nodes.yaml', 'client/clash-nodes.yaml',
            'secrets/xray-inbounds.json', 'secrets/hysteria.yaml',
            'config/hysteria/config.yaml',
        ))
        paths.extend((host / 'config/xray').glob('*.json'))
        if ansible_dir:
            paths.append(ansible_dir / f'{host.name}.yml')
    return paths


def main(argv: list[str] | None = None) -> int:
    invoked_without_args = len(sys.argv) == 1 if argv is None else len(argv) == 0
    args = parse_args(argv)
    apply_default_invocation(args, invoked_without_args)
    hosts_dir = args.hosts_dir.expanduser().resolve()
    airport_dir = (args.airport_dir or default_airport_dir(hosts_dir)).expanduser().resolve()
    trusted_nodes_file = (
        args.trusted_nodes_file or default_trusted_nodes_file(airport_dir)
    ).expanduser().resolve()
    explicit_trusted = args.trusted_nodes_file is not None or bool(os.environ.get('CLASH_TRUSTED_NODES_FILE'))
    if explicit_trusted and not trusted_nodes_file.is_file():
        raise SystemExit('显式指定的 trusted-nodes 文件不存在或不是普通文件')
    ansible_host_vars_dir = (
        args.ansible_host_vars_dir.expanduser().resolve()
        if args.ansible_host_vars_dir
        else None
    )
    counters: dict[tuple[str, str], int] = {}
    protected_inputs = input_paths(hosts_dir, airport_dir, trusted_nodes_file,
                                   ansible_host_vars_dir)
    home_template = args.home_template.expanduser().resolve()
    protected_inputs.append(home_template)
    validate_output_paths([
        ('主输出', args.output), ('raw-output', args.raw_output),
        ('loon-output', args.loon_output),
    ], protected_inputs)
    try:
        proxies = collect_proxies(hosts_dir, ansible_host_vars_dir, counters)
        trusted_nodes = load_trusted_nodes(trusted_nodes_file, counters)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if trusted_nodes:
        proxies.extend(trusted_nodes)
        print(
            f"已导入 {len(trusted_nodes)} 个 trusted-nodes.yaml 节点；"
            "nodes 格式按显式能力处理。"
        )
    if args.interactive:
        try:
            airport_nodes, _airport_dns_policy = interactive_airport_import(airport_dir, counters)
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        proxies.extend(airport_nodes)
    if not proxies:
        raise SystemExit(
            f"没有在 {hosts_dir} 或 {trusted_nodes_file} 找到节点；"
            "请检查 vps-*/host.env、trusted-nodes.yaml 或机场订阅"
        )
    if args.audit:
        for proxy in proxies:
            audit = proxy.get('_conversion-audit')
            if audit:
                print(json.dumps({'node': proxy['name'], **audit}, ensure_ascii=False))

    if args.interactive:
        proxies, output_format, chains = interactive_selection(proxies)
    else:
        before = len(proxies)
        proxies = exclude_by_patterns(proxies, args.exclude_node)
        if len(proxies) != before:
            print(f"excluded {before - len(proxies)} base nodes, {len(proxies)} remaining")
        if args.plain:
            output_format = "plain"
        elif args.merge:
            output_format = "merge"
        elif args.template or args.routes or args.chains is not None:
            output_format = "template"
        else:
            output_format = "plain"
        if output_format == "plain" or args.chains == "none":
            chains = []
        elif args.routes:
            chains = select_routes(proxies, args.routes)
        else:
            chains = chain_candidates(proxies)

    default_output = OUT if output_format == "plain" else SCRIPT_DIR / "clash-vps.generated.yaml"
    output = (args.output or default_output).expanduser()
    raw_output = args.raw_output.expanduser() if args.raw_output else None
    loon_output = args.loon_output.expanduser() if args.loon_output else None
    validate_output_paths(
        [
            ("主输出", output),
            ("raw-output", raw_output),
            ("loon-output", loon_output),
        ],
        protected_inputs,
    )

    if output_format != "plain":
        try:
            validate_generated_against_home(proxies, chains, home_template)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            raise SystemExit(f"生成结果与 home 模板不一致：{exc}") from exc

    # Render every requested format before replacing any user output. Staging
    # files contain credentials and live only in a private temporary directory.
    rendered: list[tuple[Path, str]] = []
    with tempfile.TemporaryDirectory(prefix='clash-node-render-') as staging:
        stage = Path(staging)
        label, destination = '主输出', output
        try:
            primary = stage / 'main.yaml'
            if output_format == 'plain':
                write_plain(proxies, primary)
            else:
                write_template(proxies, chains, primary)
            load_yaml(primary)
            rendered.append((output, primary.read_text(encoding='utf-8')))
            if raw_output:
                label, destination = 'raw-output', raw_output
                raw = stage / 'raw.yaml'
                write_plain(proxies, raw)
                load_yaml(raw)
                rendered.append((raw_output, raw.read_text(encoding='utf-8')))
            if loon_output:
                label, destination = 'Loon 输出', loon_output
                loon = stage / 'loon.conf'
                loon_count, loon_chain_count, loon_skipped = write_loon(
                    proxies, loon, chains, home_template
                )
                rendered.append((loon_output, loon.read_text(encoding='utf-8')))
        except (OSError, ValueError) as exc:
            raise SystemExit(f'无法写入{label} {destination}：{exc}') from exc
    validate_output_paths([('主输出', output), ('raw-output', raw_output),
                           ('loon-output', loon_output)], protected_inputs)
    for destination, content in rendered:
        try:
            secure_write(destination, content)
        except OSError as exc:
            raise SystemExit(f'无法写入输出 {destination}：{exc}') from exc
    print(
        f"wrote {output} ({len(proxies)} base nodes, {len(chains)} chains, "
        f"format={output_format})"
    )
    if raw_output:
        print(f"wrote {raw_output} ({len(proxies)} nodes, raw=True)")
    if loon_output:
        print(f"wrote {loon_output} ({loon_count} nodes, {loon_chain_count} chains, format=loon)")
        if loon_skipped:
            print(f"warning: skipped {len(loon_skipped)} Loon node(s)/chain(s):")
            for item in loon_skipped:
                print(f"  - {item}")
    else:
        print("Loon 输出已禁用（--no-loon）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
