import unittest
import os
import tempfile
import copy
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

    def test_stash_only_dns_policy_survives_regeneration(self):
        fixed = load_yaml(ROOT / "stash-dns-policy.yaml")["nameserver-policy"]
        policy = self.config["dns"]["nameserver-policy"]
        self.assertTrue(fixed)
        self.assertTrue(set(fixed).isdisjoint(self.source["dns"]["nameserver-policy"]))
        self.assertTrue(all(policy[key] == value for key, value in fixed.items()))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "stash.yaml"
            generate_stash_config.write_config(ROOT / "home.yaml", output)
            self.assertEqual(load_yaml(output), self.config)
            self.assertIn("GeositeCN 规则", output.read_text(encoding="utf-8"))

    def test_stash_only_dns_policy_rejects_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            policy_path = Path(directory) / "fixed.yaml"
            policy_path.write_text(
                "nameserver-policy:\n  'geosite:cn': 'https://dns.alidns.com/dns-query'\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "重复"):
                generate_stash_config.convert_config(
                    self.source, fixed_policy_path=policy_path
                )

    def test_domestic_dns_routes_default_to_reject_in_both_templates(self):
        target = "📡.<DNS>--ChinaDNS"
        for config in (self.source, self.config):
            with self.subTest(stash=config is self.config):
                group = next(g for g in config["proxy-groups"] if g["name"] == target)
                self.assertEqual(group["proxies"][0], "REJECT")
                self.assertIn("DIRECT", group["proxies"])
                rules = [[part.strip() for part in r.split(",")] for r in config["rules"]]
                for provider in ("ChinaDNS_Domain", "ChinaDNS_IP"):
                    self.assertTrue(any(r[0].upper() == "RULE-SET" and
                                        r[1:3] == [provider, target] for r in rules))
                self.assertTrue(config["dns"]["follow-rule"])

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
                "proxy-server-nameserver",
                "default-nameserver",
                "nameserver",
                "nameserver-policy",
                "follow-rule",
                "fake-ip-filter",
            },
        )
        self.assertEqual(
            self.config["dns"]["proxy-server-nameserver"],
            self.source["dns"]["proxy-server-nameserver"],
        )

    def test_proxy_groups_keep_structure_and_omit_unavailable_empty_fallback(self):
        valid_targets = set(self.groups) | BUILTINS
        self.assertEqual(len(self.groups), len(self.config["proxy-groups"]))
        self.assertEqual(
            {group["type"] for group in self.config["proxy-groups"]},
            {"select", "url-test", "fallback"},
        )
        self.assertEqual(
            {key for group in self.config["proxy-groups"] for key in group}
            & {"url", "expected-status", "timeout", "max-failed-times", "tolerance",
               "empty-fallback", "hidden", "exclude-filter", "include-all", "filter"},
            set(),
        )

        source_empty_groups = {
            group["name"]
            for group in self.source["proxy-groups"]
            if "empty-fallback" in group
        }
        for name in source_empty_groups:
            self.assertNotIn("empty-fallback", self.groups[name])
            source_group = next(
                group for group in self.source["proxy-groups"] if group["name"] == name
            )
            if "REJECT" not in source_group.get("proxies", []):
                self.assertNotIn("REJECT", self.groups[name].get("proxies", []), name)

        for group in self.config["proxy-groups"]:
            self.assertNotIn("PASS", group.get("proxies", []), group["name"])
            if group["type"] == "select":
                self.assertEqual(group["interval"], -1, group["name"])
            else:
                self.assertIn(group["interval"], {30, 45, 90}, group["name"])
                self.assertIs(group["lazy"], True, group["name"])
            for reference in group.get("proxies", []):
                self.assertIn(reference, valid_targets, (group["name"], reference))

    def test_public_stash_groups_have_no_runtime_node_regex(self):
        self.assertTrue(any("exclude-filter" in group for group in self.source["proxy-groups"]))
        self.assertFalse(any("filter" in group or "include-all" in group
                             for group in self.config["proxy-groups"]))

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

    def test_download_filter_contract_allows_prefix_rename(self):
        source_group = next(
            group
            for group in self.source["proxy-groups"]
            if group.get("name", "").endswith(".DirectExit-[Download]")
        )
        renamed = copy.deepcopy(source_group)
        renamed["name"] = "🧪.DirectExit-[Download]"
        generate_stash_config._validate_group_filter_contract(renamed)

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
