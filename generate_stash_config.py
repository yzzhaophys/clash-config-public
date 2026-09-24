#!/usr/bin/env python3
"""Render the public home template as a Stash-compatible, no-node config."""

from __future__ import annotations

import argparse
import copy
import ipaddress
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml

from node_io import load_yaml


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = SCRIPT_DIR / "home.yaml"
DEFAULT_OUTPUT = SCRIPT_DIR / "home-stash.yaml"
DEFAULT_FIXED_POLICY = SCRIPT_DIR / "stash-dns-policy.yaml"

# These are the public, client-side sections that are useful to Stash.  Ports,
# TUN, controller, geodata and process-sniffing settings belong to the host
# client and are intentionally not copied into an iOS/tvOS profile.
TOP_LEVEL_FIELDS = ("mode", "log-level", "hosts")

# Keep only DNS fields covered by the Stash configuration documentation and by
# the existing template's Stash path.  In particular, do not copy Clash Meta's
# listener, fake-ip mode, direct-nameserver or respect-rules extensions.
DNS_FIELDS = (
    "enable",
    "skip-cert-verify",
    "proxy-server-nameserver",
    "default-nameserver",
    "nameserver",
    "nameserver-policy",
    "follow-rule",
    "fake-ip-filter",
)

# Stash documents group interval/lazy and per-proxy benchmark-url/timeout.
# The source's group-level URL/status/timeout and Mihomo extensions are not
# relied upon by this Stash profile.
GROUP_FIELDS_TO_DROP = {
    "url", "expected-status", "timeout", "max-failed-times", "tolerance",
    "empty-fallback", "hidden", "exclude-filter", "include-all", "filter",
}

# Stash rule providers use behavior/format/url/path/interval/headers.  The
# source's type: http and provider download proxy are Mihomo extensions.
RULE_PROVIDER_FIELDS_TO_DROP = {"type", "proxy"}

RULE_HEADS = {
    "and",
    "domain",
    "domain-keyword",
    "domain-regex",
    "domain-suffix",
    "domain-wildcard",
    "geoip",
    "geosite",
    "ip-asn",
    "ip-cidr",
    "ip-cidr6",
    "match",
    "network",
    "not",
    "or",
    "protocol",
    "process-name",
    "process-path",
    "rule-set",
    "script",
    "src-ip-cidr",
    "src-port",
    "user-agent",
    "url-regex",
}

PROXY_GROUP_BUILTINS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS"}


def _copy_mapping_fields(mapping: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields:
        if field in mapping and mapping[field] is not None:
            result[field] = copy.deepcopy(mapping[field])
    return result


def _convert_hosts(hosts: Any) -> dict[str, Any]:
    if not isinstance(hosts, dict):
        raise ValueError("源配置的 hosts 必须是映射")
    converted = copy.deepcopy(hosts)
    for host, value in converted.items():
        # Stash documents a CNAME as a single hostname.  An array is for
        # multiple IP addresses, while home.yaml wraps its one CNAME in a list.
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
            try:
                ipaddress.ip_address(value[0])
            except ValueError:
                converted[host] = value[0]
    return converted


def _validate_group_filter_contract(group: dict[str, Any]) -> None:
    """Keep the audited source selectors; private rendering resolves them."""

    name = group.get("name")
    if not isinstance(name, str):
        raise ValueError("代理组名称必须是字符串")

    source_filter = group.get("filter")
    source_exclude = group.get("exclude-filter")
    if (
        source_filter == r"(?i)^VPS-.*\[Download=true\].*$"
        and source_exclude == r"(?i)PrxChain"
    ):
        # Identify Download by its filter contract rather than its decorative
        # prefix.  The public template is free to rename the group icon.
        original = source_filter
        excluded = source_exclude
    else:
        match = re.search(r"\.DirectExit-\[([A-Z]{2})(\.HomeIP|\.ShowIP)?\]$", name)
        if match is None:
            raise ValueError(f"无法转换代理组排除筛选: {name}")
        region, role = match.groups()
        if role == ".HomeIP":
            original = rf"(?i)^VPS-\[{region}\.HomeIP\]-"
        elif role == ".ShowIP":
            original = rf"(?i)^VPS-\[{region}\.(?:Core|Exit|HomeIP)\]-.*\[ShowIP=true\]$"
        else:
            original = rf"(?i)^VPS-\[{region}\.(?:Core|Exit)\]-"
        excluded = r"(?i)(?:PrxChain|Direct=false)"

    if group.get("filter") != original or group.get("exclude-filter") != excluded:
        raise ValueError(f"代理组筛选规则已变化，需要重新审核 Stash 转换: {name}")


def _validate_proxy_groups(
    groups: Any,
    *,
    allowed_builtins: set[str] = PROXY_GROUP_BUILTINS,
) -> dict[str, dict[str, Any]]:
    """Validate group names, references, filters and cycles before rendering."""

    if not isinstance(groups, list):
        raise ValueError("proxy-groups 必须是列表")

    by_name: dict[str, dict[str, Any]] = {}
    references: dict[str, list[str]] = {}
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError(f"proxy-groups[{index}] 必须是映射")
        name = group.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"proxy-groups[{index}].name 必须是非空字符串")
        if name in by_name:
            raise ValueError(f"proxy-groups 存在重复名称: {name}")
        by_name[name] = group

        for field in ("filter", "exclude-filter"):
            value = group.get(field)
            if field not in group:
                continue
            if not isinstance(value, str):
                raise ValueError(f"代理组 {name} 的 {field} 必须是字符串")
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"代理组 {name} 的 {field} 不是有效正则") from exc

        if "proxies" not in group:
            references[name] = []
            continue
        candidates = group["proxies"]
        if not isinstance(candidates, list):
            raise ValueError(f"代理组 {name} 的 proxies 必须是列表")
        if any(not isinstance(candidate, str) or not candidate for candidate in candidates):
            raise ValueError(f"代理组 {name} 的 proxies 必须只包含非空字符串")
        references[name] = candidates

    for name, candidates in references.items():
        for candidate in candidates:
            if candidate not in by_name and candidate not in allowed_builtins:
                raise ValueError(f"代理组 {name} 引用了不存在的代理组: {candidate}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise ValueError(f"代理组存在循环引用: {name}")
        visiting.add(name)
        for candidate in references[name]:
            if candidate in by_name:
                visit(candidate)
        visiting.remove(name)
        visited.add(name)

    for name in by_name:
        visit(name)
    return by_name


def _validate_rule_targets(
    rules: Any,
    groups: dict[str, dict[str, Any]],
    providers: dict[str, Any],
) -> None:
    """Reject stale group/provider names after a template rename."""

    if not isinstance(rules, list):
        raise ValueError("rules 必须是列表")
    valid_targets = set(groups) | (PROXY_GROUP_BUILTINS - {"PASS"})
    for index, rule in enumerate(rules):
        if not isinstance(rule, str) or "," not in rule:
            continue
        fields = [field.strip() for field in rule.split(",")]
        head = fields[0].upper()
        if head == "RULE-SET":
            if len(fields) < 3:
                raise ValueError(f"rules[{index}] 的 RULE-SET 缺少目标组")
            if fields[1] not in providers:
                raise ValueError(f"rules[{index}] 引用了不存在的 rule-provider: {fields[1]}")
            target = fields[2]
        elif head == "MATCH":
            if len(fields) < 2:
                raise ValueError(f"rules[{index}] 的 MATCH 缺少目标组")
            target = fields[1]
        elif head in {"AND", "OR", "NOT"}:
            target = fields[-1]
        else:
            target = fields[2] if len(fields) >= 3 else fields[-1]
        if target not in valid_targets:
            raise ValueError(f"rules[{index}] 引用了不存在的代理组: {target}")


def _normalize_rule(rule: Any) -> Any:
    """Trim the padded rule template without changing nested expressions."""

    if not isinstance(rule, str) or "," not in rule:
        return copy.deepcopy(rule)
    head, rest = rule.split(",", 1)
    normalized_head = head.strip()
    if normalized_head.lower() not in RULE_HEADS:
        return copy.deepcopy(rule)
    return f"{normalized_head.upper()},{rest.strip()}"


def _normalize_dns_server(server: Any) -> Any:
    """Remove Clash proxy-group URL fragments from a Stash DNS server."""

    if not isinstance(server, str) or "#" not in server:
        return copy.deepcopy(server)
    base, fragment = server.rsplit("#", 1)
    # Stash documents #h3=true as a DNS transport option.  The other
    # fragments in home.yaml are Clash Meta proxy-group selectors.
    if fragment.lower() in {"h3=true", "h3=false"}:
        return copy.deepcopy(server)
    return base


def _convert_nameserver_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict):
        raise ValueError("源配置的 dns.nameserver-policy 必须是映射")
    result: dict[str, Any] = {}
    for key, value in policy.items():
        if not isinstance(key, str):
            raise ValueError("dns.nameserver-policy 的键必须是字符串")
        # Stash's documented nameserver-policy matchers are exact domains,
        # wildcards and geosite sets.  rule-set: entries are a Clash Meta
        # extension and remain available through ordinary rules/RULE-SET.
        if key.lower().startswith("rule-set:"):
            continue
        if isinstance(value, list):
            result[key] = [_normalize_dns_server(item) for item in value]
        else:
            result[key] = _normalize_dns_server(value)
    return result


def _load_fixed_policy(path: Path) -> dict[str, Any]:
    document = load_yaml(path)
    if not isinstance(document, dict) or set(document) != {"nameserver-policy"}:
        raise ValueError("Stash 固定 DNS 文件只能包含 nameserver-policy 映射")
    policy = document["nameserver-policy"]
    if not isinstance(policy, dict):
        raise ValueError("Stash 固定 nameserver-policy 必须是映射")
    for key, value in policy.items():
        if not isinstance(key, str) or not key or key == "+.*" or key.lower().startswith("rule-set:"):
            raise ValueError("Stash 固定 DNS 规则键无效")
        servers = value if isinstance(value, list) else [value]
        if not servers or any(not isinstance(server, str) or not server.strip() for server in servers):
            raise ValueError("Stash 固定 DNS 服务器必须是非空字符串或列表")
    return policy


def convert_config(source: dict[str, Any], *,
                   fixed_policy_path: Path = DEFAULT_FIXED_POLICY) -> dict[str, Any]:
    """Convert a loaded public Clash/Mihomo template to a Stash template.

    Nodes and proxy providers are deliberately not copied.  Proxy-group
    definitions are retained as an empty structural skeleton so that a later
    private node render can populate the same names and filters.
    """

    if not isinstance(source, dict):
        raise ValueError("源配置必须是 YAML 映射")

    output: dict[str, Any] = _copy_mapping_fields(source, TOP_LEVEL_FIELDS)
    if "hosts" in output:
        output["hosts"] = _convert_hosts(output["hosts"])

    source_dns = source.get("dns")
    if not isinstance(source_dns, dict):
        raise ValueError("源配置缺少有效的 dns 映射")
    output["dns"] = _copy_mapping_fields(source_dns, DNS_FIELDS)
    if "nameserver-policy" not in source_dns:
        raise ValueError("源配置缺少 dns.nameserver-policy")
    output["dns"]["nameserver-policy"] = _convert_nameserver_policy(
        source_dns["nameserver-policy"]
    )
    # In Stash wildcard policies outrank geosite. Use nameserver as the
    # fallback instead, otherwise +.* shadows every geosite policy.
    policy = output["dns"]["nameserver-policy"]
    if "+.*" in policy:
        fallback = policy.pop("+.*")
        if not isinstance(fallback, (str, list)) or not fallback:
            raise ValueError("DNS 兜底必须是非空服务器或列表")
        servers = fallback if isinstance(fallback, list) else [fallback]
        if any(not isinstance(server, str) or not server.strip() for server in servers):
            raise ValueError("DNS 兜底服务器必须是非空字符串")
        output["dns"]["nameserver"] = servers

    fixed_policy = _load_fixed_policy(fixed_policy_path)
    if fixed_policy.keys() & policy.keys():
        raise ValueError("Stash 固定 DNS 规则与 home.yaml 重复")
    output["dns"]["nameserver-policy"] = {**fixed_policy, **policy}

    # Omit node/provider sections. This converter is for the public template,
    # not a general credential scrubber for arbitrary private configurations.
    output["proxies"] = []
    output["proxy-providers"] = {}

    groups = source.get("proxy-groups")
    if not isinstance(groups, list):
        raise ValueError("源配置缺少有效的 proxy-groups 列表")
    _validate_proxy_groups(groups)
    output_groups: list[dict[str, Any]] = []
    for group in groups:
        converted = {
            key: copy.deepcopy(value)
            for key, value in group.items()
            if key not in GROUP_FIELDS_TO_DROP
        }
        if "proxies" in converted:
            # Stash documents DIRECT, REJECT and REJECT-DROP.  The source's
            # manual PASS option is not a documented Stash outbound.
            converted["proxies"] = [
                candidate for candidate in converted["proxies"] if candidate != "PASS"
            ]
        if converted.get("type") == "select":
            # Stash otherwise schedules tests for select groups every 600s,
            # including nested groups.  Let the contained automatic groups
            # keep their own schedules without duplicate recursion.
            converted["interval"] = -1
        if any(field in group for field in ("include-all", "filter", "exclude-filter")):
            if (group.get("include-all") is not True
                    or not isinstance(group.get("filter"), str)
                    or group.get("empty-fallback") != "REJECT"):
                raise ValueError("未审查的自动节点筛选组，无法生成静态 Stash 骨架")
            if "exclude-filter" in group:
                _validate_group_filter_contract(group)
            elif ".Chain-[" not in group["name"]:
                raise ValueError("未审查的自动节点筛选组，无法生成静态 Stash 骨架")
        if "empty-fallback" in group:
            if group["empty-fallback"] != "REJECT":
                raise ValueError("Stash 转换只支持 empty-fallback: REJECT")
            # Stash has no documented empty-fallback equivalent.  Adding a
            # REJECT candidate here did not make it appear in empty automatic
            # groups at runtime (Stash 3.4.1 treated them as DIRECT), so omit
            # the source-only field without claiming the fallback was kept.
        output_groups.append(converted)
    output_group_map = _validate_proxy_groups(
        output_groups,
        allowed_builtins=PROXY_GROUP_BUILTINS - {"PASS"},
    )
    output["proxy-groups"] = output_groups

    rules = source.get("rules")
    if not isinstance(rules, list):
        raise ValueError("源配置缺少有效的 rules 列表")
    output["rules"] = [_normalize_rule(rule) for rule in rules]

    providers = source.get("rule-providers")
    if not isinstance(providers, dict):
        raise ValueError("源配置缺少有效的 rule-providers 映射")
    output_providers: dict[str, Any] = {}
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            raise ValueError(f"rule-providers.{name} 不是映射")
        if provider.get("type") != "http":
            raise ValueError("Stash 转换目前只支持 HTTP rule-provider")
        output_providers[name] = {
            key: copy.deepcopy(value)
            for key, value in provider.items()
            if key not in RULE_PROVIDER_FIELDS_TO_DROP
        }
    output["rule-providers"] = output_providers
    _validate_rule_targets(output["rules"], output_group_map, output_providers)

    return output


def render_config(config: dict[str, Any], *,
                  fixed_policy_path: Path = DEFAULT_FIXED_POLICY) -> str:
    header = (
        "# Generated from home.yaml for Stash.\n"
        "# Nodes, proxy providers and generated proxy chains are intentionally omitted.\n"
        "# Add private nodes before enabling this profile; empty automatic groups follow Stash's DIRECT behavior.\n"
    )
    fixed_policy = _load_fixed_policy(fixed_policy_path)
    base = copy.deepcopy(config)
    policy = base["dns"]["nameserver-policy"]
    for key in fixed_policy:
        if key not in policy or policy[key] != fixed_policy[key]:
            raise ValueError("Stash 固定 DNS 规则与待渲染配置不一致")
        del policy[key]
    rendered = header + yaml.safe_dump(
        base,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=120,
    )
    raw = fixed_policy_path.read_text(encoding="utf-8")
    heading, separator, body = raw.partition("\n")
    if heading != "nameserver-policy:" or not separator or not body.endswith("\n"):
        raise ValueError("Stash 固定 DNS 文件格式无效")
    marker = "  nameserver-policy:\n"
    if rendered.count(marker) != 1:
        raise ValueError("生成的 Stash DNS 结构无效")
    return rendered.replace(marker, marker + body, 1)


def _check_output_path(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve():
        raise ValueError("源文件和输出文件不能相同")
    if destination.exists() or destination.is_symlink():
        info = destination.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"拒绝覆盖符号链接: {destination}")
        if info.st_nlink != 1:
            raise ValueError(f"拒绝覆盖硬链接输出: {destination}")
        if os.path.samefile(source, destination):
            raise ValueError("源文件和输出文件是同一个文件")
    if not destination.parent.is_dir():
        raise ValueError(f"输出目录不存在: {destination.parent}")


def write_config(source_path: Path, output_path: Path, *,
                 fixed_policy_path: Path = DEFAULT_FIXED_POLICY) -> None:
    _check_output_path(source_path, output_path)
    source = load_yaml(source_path)
    config = convert_config(source, fixed_policy_path=fixed_policy_path)
    rendered = render_config(config, fixed_policy_path=fixed_policy_path)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        # Validate before replacing an existing output.  The strict loader
        # rejects duplicate keys while still accepting legal YAML anchors.
        if load_yaml(temporary_path) != config:
            raise ValueError("生成的 Stash DNS 规则与固定输入不一致")
        os.replace(temporary_path, output_path)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    write_config(args.source, args.output)
    print(f"已生成 Stash 配置: {args.output}")


if __name__ == "__main__":
    main()
