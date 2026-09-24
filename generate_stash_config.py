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
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from node_io import load_yaml


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = SCRIPT_DIR / "home.yaml"
DEFAULT_OUTPUT = SCRIPT_DIR / "home-stash.yaml"

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
    "empty-fallback", "hidden", "exclude-filter",
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


@lru_cache(maxsize=4)
def _without_words(words: tuple[str, ...], single_line: bool) -> str:
    """Compile literal substring exclusions without lookarounds (RE2-safe).

    States track the longest suffix that is a proper prefix of a forbidden
    word. Completing any word discards the transition. All remaining states
    accept; state elimination converts this finite automaton to a regex.
    Callers apply (?i), and only pass the audited ASCII words below.
    """
    words = tuple(word.lower() for word in words)
    states = sorted({word[:i] for word in words for i in range(len(word))},
                    key=lambda item: (len(item), item))
    alphabet = sorted(set("".join(words)))
    edges: dict[tuple[int, int], str] = {}

    def add(left: int, right: int, regex: str) -> None:
        previous = edges.get((left, right))
        if previous is None:
            edges[left, right] = regex
        elif previous != regex:
            edges[left, right] = f"(?:{previous}|{regex})"

    start, end = len(states), len(states) + 1
    add(start, 0, "")
    for i, prefix in enumerate(states):
        destinations: dict[int, list[str]] = {}
        for char in alphabet:
            text = prefix + char
            if any(text.endswith(word) for word in words):
                continue
            target = max((state for state in states if text.endswith(state)), key=len)
            destinations.setdefault(states.index(target), []).append(char)
        for target, chars in destinations.items():
            add(i, target, "[" + "".join(chars) + "]")
        other = "".join(alphabet) + (r"\n" if single_line else "")
        add(i, 0, f"[^{other}]")
        add(i, end, "")

    remaining = set(range(len(states)))
    while remaining:
        # Eliminate sparsely connected states first to keep expressions small.
        def cost(state: int) -> tuple[int, int]:
            incoming = [len(v) + 1 for (a, b), v in edges.items() if a != state and b == state]
            outgoing = [len(v) + 1 for (a, b), v in edges.items() if a == state and b != state]
            return sum(incoming) * len(outgoing) + sum(outgoing) * len(incoming), state

        # The empty-prefix state is the shared reset hub; eliminate it last.
        state = min(remaining - {0} or remaining, key=cost)
        incoming = [(a, value) for (a, b), value in edges.items() if b == state and a != state]
        outgoing = [(b, value) for (a, b), value in edges.items() if a == state and b != state]
        loop = edges.get((state, state))
        repeat = f"(?:{loop})*" if loop else ""
        for left, before in incoming:
            for right, after in outgoing:
                add(left, right, before + repeat + after)
        edges = {key: value for key, value in edges.items() if state not in key}
        remaining.remove(state)
    return "(?:" + edges[start, end] + ")"


def _convert_group_filter(group: dict[str, Any]) -> str:
    """Express the current DirectExit exclusions in Stash's documented filter."""

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
        safe_part = _without_words(("PrxChain",), True)
        result = rf"(?i)^VPS-{safe_part}\[Download=true\]{safe_part}$"
    else:
        match = re.search(r"\.DirectExit-\[([A-Z]{2})(\.HomeIP|\.ShowIP)?\]$", name)
        if match is None:
            raise ValueError(f"无法转换代理组排除筛选: {name}")
        region, role = match.groups()
        suffix = _without_words(("PrxChain", "Direct=false"), role == ".ShowIP")
        if role == ".HomeIP":
            original = rf"(?i)^VPS-\[{region}\.HomeIP\]-"
            result = original + suffix + "$"
        elif role == ".ShowIP":
            original = rf"(?i)^VPS-\[{region}\.(?:Core|Exit|HomeIP)\]-.*\[ShowIP=true\]$"
            result = rf"(?i)^VPS-\[{region}\.(?:Core|Exit|HomeIP)\]-{suffix}\[ShowIP=true\]$"
        else:
            original = rf"(?i)^VPS-\[{region}\.(?:Core|Exit)\]-"
            result = original + suffix + "$"
        excluded = r"(?i)(?:PrxChain|Direct=false)"

    if group.get("filter") != original or group.get("exclude-filter") != excluded:
        raise ValueError(f"代理组筛选规则已变化，需要重新审核 Stash 转换: {name}")
    return result


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


def convert_config(source: dict[str, Any]) -> dict[str, Any]:
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
        if "exclude-filter" in group:
            converted["filter"] = _convert_group_filter(group)
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


def render_config(config: dict[str, Any]) -> str:
    header = (
        "# Generated from home.yaml for Stash.\n"
        "# Nodes, proxy providers and generated proxy chains are intentionally omitted.\n"
        "# Add private nodes before enabling this profile; empty automatic groups follow Stash's DIRECT behavior.\n"
    )
    return header + yaml.safe_dump(
        config,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=120,
    )


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


def write_config(source_path: Path, output_path: Path) -> None:
    _check_output_path(source_path, output_path)
    source = load_yaml(source_path)
    rendered = render_config(convert_config(source))

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
        load_yaml(temporary_path)
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
