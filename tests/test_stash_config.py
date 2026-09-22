import unittest
import os
import re
import tempfile
import copy
import random
import stat
from unittest.mock import patch
from pathlib import Path

import generate_stash_config
from node_io import load_yaml


ROOT = Path(__file__).resolve().parents[1]
BUILTINS = {"DIRECT", "REJECT", "REJECT-DROP"}


class StashConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = load_yaml(ROOT / "home.yaml")
        cls.config = load_yaml(ROOT / "home-stash.yaml")
        cls.groups = {group["name"]: group for group in cls.config["proxy-groups"]}

    def test_generated_file_matches_current_source(self):
        self.assertEqual(self.config, generate_stash_config.convert_config(self.source))

    def test_public_shell_has_no_nodes_or_provider_urls(self):
        self.assertEqual(self.config["proxies"], [])
        self.assertEqual(self.config["proxy-providers"], {})
        self.assertNotIn("mixed-port", self.config)
        self.assertNotIn("external-controller", self.config)
        self.assertNotIn("secret", self.config)
        self.assertNotIn("tun", self.config)
        self.assertNotIn("sniffer", self.config)

    def test_cname_host_is_rendered_as_scalar(self):
        source = copy.deepcopy(self.source)
        source["hosts"]["cname.example"] = ["target.example.cloud"]
        converted = generate_stash_config.convert_config(source)
        self.assertEqual(converted["hosts"]["cname.example"], "target.example.cloud")

    def test_dns_catchall_becomes_default_without_shadowing_geosite(self):
        policy = self.config["dns"]["nameserver-policy"]
        self.assertNotIn("+.*", policy)
        self.assertEqual(self.config["dns"]["nameserver"], self.source["dns"]["nameserver"])
        self.assertEqual([k for k in policy if k.startswith("geosite:")],
                         [k for k in self.source["dns"]["nameserver-policy"] if k.startswith("geosite:")])
        source = copy.deepcopy(self.source)
        source["dns"]["nameserver-policy"]["+.*"] = "https://1.1.1.1/dns-query#Proxy"
        converted = generate_stash_config.convert_config(source)
        self.assertEqual(converted["dns"]["nameserver"], ["https://1.1.1.1/dns-query"])
        del source["dns"]["nameserver-policy"]["+.*"]
        self.assertEqual(generate_stash_config.convert_config(source)["dns"]["nameserver"],
                         source["dns"]["nameserver"])

    def test_invalid_dns_fallback_is_rejected(self):
        for value in (None, "", [], 0, False, [None], [""], ["1.1.1.1", {}]):
            with self.subTest(value=value):
                source = copy.deepcopy(self.source)
                source["dns"]["nameserver-policy"]["+.*"] = value
                with self.assertRaises(ValueError):
                    generate_stash_config.convert_config(source)

    def test_reject_guard_survives_group_filter(self):
        for group in self.config["proxy-groups"]:
            if "REJECT" in group.get("proxies", []) and "filter" in group:
                self.assertIsNotNone(re.search(group["filter"], "REJECT"), group["name"])

    def test_top_level_and_dns_fields_are_stash_profile_fields(self):
        self.assertEqual(
            set(self.config),
            {
                "mode",
                "log-level",
                "hosts",
                "dns",
                "proxies",
                "proxy-providers",
                "proxy-groups",
                "rules",
                "rule-providers",
            },
        )
        policy = self.config["dns"]["nameserver-policy"]
        self.assertTrue(policy)
        self.assertTrue(all(not key.lower().startswith("rule-set:") for key in policy))
        for value in policy.values():
            values = value if isinstance(value, list) else [value]
            for server in values:
                if isinstance(server, str) and "#" in server:
                    self.assertRegex(server, r"#h3=(?:true|false)$")
        self.assertEqual(
            set(self.config["dns"]),
            {
                "enable",
                "skip-cert-verify",
                "default-nameserver",
                "nameserver",
                "nameserver-policy",
                "follow-rule",
                "fake-ip-filter",
            },
        )

    def test_proxy_groups_keep_structure_and_guard_empty_auto_groups(self):
        valid_targets = set(self.groups) | BUILTINS
        self.assertEqual(len(self.groups), len(self.config["proxy-groups"]))
        self.assertEqual(
            {group["type"] for group in self.config["proxy-groups"]},
            {"select", "url-test", "fallback"},
        )
        self.assertEqual(
            {key for group in self.config["proxy-groups"] for key in group}
            & {"url", "expected-status", "timeout", "max-failed-times", "tolerance",
               "empty-fallback", "hidden", "exclude-filter"},
            set(),
        )

        source_empty_groups = {
            group["name"]
            for group in self.source["proxy-groups"]
            if "empty-fallback" in group
        }
        for name in source_empty_groups:
            self.assertIn("REJECT", self.groups[name].get("proxies", []))

        for group in self.config["proxy-groups"]:
            self.assertNotIn("PASS", group.get("proxies", []), group["name"])
            if group["type"] == "select":
                self.assertEqual(group["interval"], -1, group["name"])
            else:
                self.assertIn(group["interval"], {30, 45, 90}, group["name"])
                self.assertIs(group["lazy"], True, group["name"])
            for reference in group.get("proxies", []):
                self.assertIn(reference, valid_targets, (group["name"], reference))

    def test_direct_exit_filters_match_source_for_capability_markers(self):
        candidates = (
            "VPS-[JP.Core]-VLESS-00-(日本核心节点)-[Download=true]",
            "VPS-[JP.Core]-VLESS-00-(日本核心节点)-[Direct=false]-[Download=true]",
            "VPS-[JP.Core]-VLESS-00-(日本核心节点)-[Download=true]-[ShowIP=true]",
            "VPS-[JP.HomeIP]-H2-00-(日本住宅节点)-[ShowIP=true]",
            "VPS-[US.Exit]-VLESS-00-(美国机场出口)-[Airport=example]",
            "VPS-[US.Exit]-VLESS-01-(美国出口节点)-[Direct=false]",
            "PrxChain-[JP]-VLESS-00--<<-US.Exit.VLESS.00-(代理链)",
            "VPS-[JP.Exit]-VLESS-00-(PrxChain)-[Download=true]",
            "VPS-[JP.Exit]-VLESS-00-(example)-[Download=true]-[PrxChain=true]",
            "VPS-[JP.Exit]-VLESS-00-(example)-[Airport=Direct=false]",
            "VPS-[JP.Exit]-VLESS-00-(example)-[Custom=true]",
            "VPS-[JP.Exit]-",
            "VPS-[ZZ.Other]-[Custom=true]-[Download=true]",
            "vps-[jp.exit]-pRxChAiN-[Download=true]",
            "VPS-[JP.HomeIP]-Direct=false-[ShowIP=true]",
        )
        source_groups = {group["name"]: group for group in self.source["proxy-groups"]}
        converted_groups = self.groups
        for name, source_group in source_groups.items():
            if "exclude-filter" not in source_group:
                continue
            converted = converted_groups[name]
            for candidate in candidates:
                expected = bool(
                    re.search(source_group["filter"], candidate)
                    and not re.search(source_group["exclude-filter"], candidate)
                )
                actual = bool(re.search(converted["filter"], candidate))
                self.assertEqual(actual, expected, (name, candidate))

    def test_exclusion_automaton_against_literal_search(self):
        rng = random.Random(913)
        for words in [("PrxChain", "Direct=false"), ("PrxChain", "HomeIP", "ShowIP", "Direct=false")]:
            for single_line in (True, False):
                pattern = generate_stash_config._without_words(words, single_line)
                self.assertNotIn("(?=", pattern)
                self.assertNotIn("(?!", pattern)
                regex = re.compile(pattern, re.I)
                candidates = ["", "中文-[Custom=true]", "PrxPrxChain", "ShowHomeIP", "\n"]
                for word in words:
                    for i in range(len(word) + 1):
                        candidates.extend([word[:i], word[:i] + word, word[:i] + "-" + word[i:]])
                for _ in range(1000):
                    parts = [rng.choice(words + ("abc", "中文", "-", "\n", "[Custom=true]"))
                             for _ in range(rng.randrange(1, 6))]
                    text = "".join(part[:rng.randrange(len(part) + 1)] for part in parts)
                    candidates.append(text.swapcase())
                for text in candidates:
                    expected = not any(word.lower() in text.lower() for word in words)
                    expected &= not (single_line and "\n" in text)
                    self.assertEqual(regex.fullmatch(text) is not None, expected, repr(text))

    def test_source_rules_order_and_group_graph_preserved(self):
        self.assertEqual(list(self.groups), [g["name"] for g in self.source["proxy-groups"]])
        def tokens(rule):
            head, *rest = (token.strip() for token in rule.split(","))
            return [head.upper(), *rest]
        self.assertEqual([tokens(r) for r in self.source["rules"]],
                         [tokens(r) for r in self.config["rules"]])
        active, complete = set(), set()
        def visit(name):
            self.assertNotIn(name, active, name)
            if name in complete or name not in self.groups:
                return
            active.add(name)
            for target in self.groups[name].get("proxies", []):
                visit(target)
            active.remove(name)
            complete.add(name)
        for name in self.groups:
            visit(name)

    def test_unreviewed_source_semantics_are_rejected(self):
        for kind in ("filter", "empty-fallback", "provider"):
            source = copy.deepcopy(self.source)
            if kind == "provider":
                next(iter(source["rule-providers"].values()))["type"] = "file"
            else:
                group = next(g for g in source["proxy-groups"] if "exclude-filter" in g)
                group[kind] = "DIRECT" if kind == "empty-fallback" else ".*"
            with self.assertRaises(ValueError):
                generate_stash_config.convert_config(source)

    def test_filter_conversion_uses_download_contract_after_prefix_rename(self):
        source_group = next(
            group
            for group in self.source["proxy-groups"]
            if group.get("name", "").endswith(".DirectExit-[Download]")
        )
        renamed = copy.deepcopy(source_group)
        renamed["name"] = "🧪.DirectExit-[Download]"
        converted = generate_stash_config._convert_group_filter(renamed)
        self.assertIsNotNone(
            re.search(converted, "VPS-[US.HomeIP]-VLESS-00-(住宅)-[Download=true]")
        )
        self.assertIsNone(
            re.search(converted, "VPS-[US.HomeIP]-PrxChain-[Download=true]")
        )

    def test_group_reference_validation_rejects_stale_name_after_rename(self):
        source = copy.deepcopy(self.source)
        source["proxy-groups"][0]["name"] = "renamed-group"
        with self.assertRaisesRegex(ValueError, "不存在的代理组"):
            generate_stash_config.convert_config(source)

    def test_rule_providers_use_stash_fields(self):
        self.assertEqual(len(self.config["rule-providers"]), len(self.source["rule-providers"]))
        for name, provider in self.config["rule-providers"].items():
            self.assertNotIn("type", provider, name)
            self.assertNotIn("proxy", provider, name)
            self.assertIn("behavior", provider, name)
            self.assertIn("format", provider, name)
            self.assertIn("url", provider, name)
            self.assertIn("path", provider, name)

    def test_rules_reference_existing_stash_groups_and_rule_sets(self):
        valid_targets = set(self.groups) | BUILTINS
        providers = self.config["rule-providers"]
        for rule in self.config["rules"]:
            fields = [field.strip() for field in rule.split(",")]
            head = fields[0].upper()
            if head == "RULE-SET":
                self.assertIn(fields[1], providers, rule)
                target = fields[2]
            elif head == "MATCH":
                target = fields[1]
            elif head in {"AND", "OR", "NOT"}:
                target = fields[-1]
            else:
                target = fields[2] if len(fields) >= 3 else fields[-1]
            self.assertIn(target, valid_targets, rule)

    def test_writer_does_not_replace_output_when_source_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "invalid.yaml"
            output = root / "home-stash.yaml"
            source.write_text("mode: rule\nmode: global\n", encoding="utf-8")
            output.write_text("sentinel\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                generate_stash_config.write_config(source, output)

            self.assertEqual(output.read_text(encoding="utf-8"), "sentinel\n")

    def test_writer_rejects_symlink_and_hardlink_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.yaml"
            source.write_text((ROOT / "home.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            before = source.read_bytes()
            with self.assertRaises(ValueError):
                generate_stash_config.write_config(source, source)
            self.assertEqual(source.read_bytes(), before)

            target = root / "target.yaml"
            target.write_text("sentinel\n", encoding="utf-8")
            symlink = root / "symlink.yaml"
            os.symlink(target, symlink)
            with self.assertRaises(ValueError):
                generate_stash_config.write_config(source, symlink)
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")

            hardlink = root / "hardlink.yaml"
            os.link(target, hardlink)
            with self.assertRaises(ValueError):
                generate_stash_config.write_config(source, hardlink)
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel\n")

    def test_writer_is_private_and_failed_replace_keeps_old_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "stash.yaml"
            output.write_text("sentinel\n", encoding="utf-8")
            with patch.object(generate_stash_config.os, "replace", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    generate_stash_config.write_config(ROOT / "home.yaml", output)
            self.assertEqual(output.read_text(), "sentinel\n")
            self.assertEqual(list(Path(directory).iterdir()), [output])
            generate_stash_config.write_config(ROOT / "home.yaml", output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
