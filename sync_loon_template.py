#!/usr/bin/env python3
"""Synchronize the public Loon template's business groups and remote rules."""

import argparse
import os
import re
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote, urlsplit

import yaml

from node_io import UniqueKeyLoader


ROOT = Path(__file__).resolve().parent
HOME = ROOT / "home.yaml"
TEMPLATE = ROOT / "Loon-home.template.lcf"
LIST_BASE = "https://raw.githubusercontent.com/yzzhaophys/loon-rules/main/lists"
PROVIDER_COMMENT = re.compile(r"^(  )#\s*([\w.!@-]+):\s*(\{.*)$")
RULE_COMMENT = re.compile(r"^(  )#\s*(-\s*RULE-SET,.*)$")
START_GROUPS = "# @sync-home:groups:start"
END_GROUPS = "# @sync-home:groups:end"
START_RULES = "# @sync-home:rules:start"
END_RULES = "# @sync-home:rules:end"
DYNAMIC_MARKERS = (
    "# @generate:route", "# @generate:line",
    "# @generate:chain", "# @generate:direct-exit",
)
SENSITIVE_KEY = re.compile(
    r"(?i)^\s*(?:ca-p12|ca-passphrase|password|passwd|private-key|secret|"
    r"token|api[-_]?key|authorization|uuid)\s*="
)


class PendingRuleLists(ValueError):
    """The published rule lists have not caught up with home.yaml yet."""


def home_data(path: Path):
    lines = path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    provider_start = next((i for i, x in enumerate(lines) if x.strip() == "rule-providers:"), None)
    rules_start = next((i for i, x in enumerate(lines) if x.strip() == "rules:"), None)
    if provider_start is None or rules_start is None or provider_start >= rules_start:
        raise ValueError("home.yaml needs rule-providers before rules")
    for i in range(provider_start + 1, rules_start):
        match = PROVIDER_COMMENT.match(lines[i])
        if match:
            lines[i] = f"{match[1]}{match[2]}: {match[3].rstrip()}\n"
    for i in range(rules_start + 1, len(lines)):
        match = RULE_COMMENT.match(lines[i])
        if match:
            lines[i] = f"{match[1]}{match[2].rstrip()}\n"
    try:
        data = yaml.load("".join(lines), Loader=UniqueKeyLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}" if mark else ""
        raise ValueError(f"invalid home.yaml{location}") from None
    if not isinstance(data, dict):
        raise ValueError("home.yaml must be a mapping")
    return data


def render_groups(groups):
    if not isinstance(groups, list) or not groups:
        raise ValueError("proxy-groups must be a nonempty list")
    business = []
    dynamic_started = False
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("name"), str):
            raise ValueError("invalid proxy group")
        if group.get("type") != "select":
            dynamic_started = True
            continue
        if dynamic_started:
            raise ValueError("business groups must precede dynamic groups")
        name = group["name"]
        members = group.get("proxies")
        icon = group.get("icon")
        if (not name or any(c in name for c in ",=\n\r") or
                not isinstance(members, list) or not members or
                not all(isinstance(x, str) and x and not any(c in x for c in ",\n\r") for x in members) or
                not isinstance(icon, str) or not icon.startswith("https://") or "," in icon):
            raise ValueError(f"invalid Loon business group: {name!r}")
        business.append(f"{name} = select,{','.join(members)},img-url={icon}")
    if not business or len({x.split(" = ", 1)[0] for x in business}) != len(business):
        raise ValueError("business group names are empty or duplicated")
    return business


def render_rules(rules, providers):
    if not isinstance(rules, list) or not isinstance(providers, dict):
        raise ValueError("rules and rule-providers must be lists and mappings")
    rendered = []
    for rule in rules:
        if not isinstance(rule, str):
            raise ValueError("rules must contain strings")
        fields = [part.strip() for part in rule.split(",")]
        if fields[0] == "MATCH":
            if len(fields) != 2 or fields[1] != "Final":
                raise ValueError("MATCH must point to Final")
            continue
        if fields[0] != "RULE-SET" or len(fields) not in (3, 4):
            raise ValueError("unsupported rule in Loon template")
        _, name, policy, *options = fields
        if (name not in providers or not name or not policy or
                any(c in policy for c in ",\n\r") or
                (options and options != ["no-resolve"])):
            raise ValueError(f"invalid RULE-SET reference: {name!r}")
        provider = providers[name]
        if not isinstance(provider, dict) or provider.get("behavior") not in ("domain", "ipcidr"):
            raise ValueError(f"unsupported provider: {name!r}")
        url = f"{LIST_BASE}/{quote(name, safe='')}.list"
        rendered.append(f"{url}, policy={policy}, tag={name}, enabled=true")
    if not rendered:
        raise ValueError("no RULE-SET rules")
    return rendered


def replace_block(lines, start_marker, end_marker, rows, section):
    start = [i for i, x in enumerate(lines) if x.strip() == start_marker]
    end = [i for i, x in enumerate(lines) if x.strip() == end_marker]
    if len(start) != 1 or len(end) != 1 or start[0] >= end[0]:
        raise ValueError(f"missing or duplicated {section} sync markers")
    section_start = max((i for i in range(start[0] + 1) if lines[i].startswith("[")), default=-1)
    section_end = next((i for i in range(end[0] + 1, len(lines)) if lines[i].startswith("[")), len(lines))
    if section_start < 0 or lines[section_start].strip() != section or end[0] >= section_end:
        raise ValueError(f"sync markers must stay inside {section}")
    return lines[:start[0] + 1] + [x + "\n" for x in rows] + lines[end[0]:]


def validate_public_template(content):
    section = ""
    for number, line in enumerate(content.splitlines(), 1):
        entry = line.strip()
        if entry.startswith("[") and entry.endswith("]"):
            section = entry
            continue
        if not entry or entry.startswith(("#", ";")):
            continue
        if section in ("[Proxy]", "[Proxy Chain]") or SENSITIVE_KEY.match(entry):
            raise ValueError(f"private content in public template at line {number}")
        if "-----BEGIN " in entry:
            raise ValueError(f"certificate or key in public template at line {number}")
        for url in re.findall(r"https?://[^\s,]+", entry):
            parsed = urlsplit(url)
            if parsed.username or parsed.password or parsed.query:
                raise ValueError(f"credential-bearing URL in public template at line {number}")


def render(home: Path, template: Path, lists_dir: Path | None = None):
    data = home_data(home)
    if lists_dir is not None:
        expected = {f"{name}.list" for name in data.get("rule-providers", {})}
        actual = {path.name for path in lists_dir.glob("*.list")}
        if not expected or expected != actual:
            raise PendingRuleLists(
                f"rule lists are not synchronized: {len(expected - actual)} missing, "
                f"{len(actual - expected)} extra"
            )
    groups = render_groups(data.get("proxy-groups"))
    rules = render_rules(data.get("rules"), data.get("rule-providers"))
    original = template.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    for marker in DYNAMIC_MARKERS:
        if sum(x.strip() == marker for x in lines) != 1:
            raise ValueError(f"missing or duplicated {marker}")
    lines = replace_block(lines, START_GROUPS, END_GROUPS, groups, "[Proxy Group]")
    lines = replace_block(lines, START_RULES, END_RULES, rules, "[Remote Rule]")
    result = "".join(lines)
    validate_public_template(result)
    return result, len(groups), len(rules)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=HOME)
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    parser.add_argument("--lists-dir", type=Path, help="require the published list names to match home.yaml")
    parser.add_argument("--wait-for-lists", action="store_true", help="succeed without changes while published lists catch up")
    parser.add_argument("--check", action="store_true", help="fail if the template needs updating")
    args = parser.parse_args()
    if args.wait_for_lists and args.lists_dir is None:
        parser.error("--wait-for-lists requires --lists-dir")
    if args.template.is_symlink() or args.template.stat().st_nlink != 1:
        parser.error("template cannot be a symlink or hardlink")
    if args.home.resolve() == args.template.resolve():
        parser.error("home and template must be different files")
    try:
        content, groups, rules = render(args.home, args.template, args.lists_dir)
    except PendingRuleLists as exc:
        if args.wait_for_lists:
            print(f"Waiting for published Loon lists: {exc}")
            return
        parser.error(str(exc))
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    old = args.template.read_text(encoding="utf-8")
    if content == old:
        print(f"Template current: {groups} groups, {rules} remote rules")
        return
    if args.check:
        parser.exit(1, "Loon template is out of sync with home.yaml\n")
    mode = stat.S_IMODE(args.template.stat().st_mode)
    fd, name = tempfile.mkstemp(prefix=".loon-template-", dir=args.template.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, args.template)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print(f"Updated template: {groups} groups, {rules} remote rules")


if __name__ == "__main__":
    main()
