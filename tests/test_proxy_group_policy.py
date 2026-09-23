"""Guard the template's failover graph and download routing boundaries."""

import re
import unittest
from pathlib import Path

import generate_raw_nodes as generator
from generate_raw_nodes import node_name
from node_io import load_yaml


class ProxyGroupPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_yaml(Path(__file__).resolve().parents[1] / "home.yaml")
        cls.groups = {group["name"]: group for group in cls.config["proxy-groups"]}

    def test_group_references_resolve_without_cycles(self):
        self.assertEqual(len(self.groups), len(self.config["proxy-groups"]))
        terminals = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}
        terminals.update(proxy["name"] for proxy in self.config.get("proxies", []))
        visited = set()

        def visit(name, path):
            if name in terminals:
                return
            self.assertIn(name, self.groups, f"Missing group referenced from {path}")
            self.assertNotIn(name, path, f"Group cycle: {path} -> {name}")
            if name in visited:
                return
            for child in self.groups[name].get("proxies", []):
                visit(child, path + [name])
            visited.add(name)

        for name in self.groups:
            visit(name, [])

    def test_rule_targets_reference_existing_groups_or_builtin_outbounds(self):
        targets = set(self.groups) | {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}
        for rule in self.config["rules"]:
            parts = [part.strip() for part in rule.split(",")]
            target = parts[-2] if parts[-1].lower() == "no-resolve" else parts[-1]
            with self.subTest(rule=rule):
                self.assertIn(target, targets)

    def test_automatic_pools_have_lazy_bounded_health_checks(self):
        automatic = [g for g in self.groups.values() if g["type"] != "select"]
        self.assertTrue(automatic)
        for group in automatic:
            with self.subTest(group=group["name"]):
                self.assertIn(group["type"], {"url-test", "fallback"})
                self.assertIs(group["lazy"], True)
                self.assertIn(group["interval"], {30, 45, 90})
                self.assertIn(group["timeout"], {3000, 4000, 5000})
                self.assertGreater(group["timeout"], 0)
                self.assertLess(group["timeout"], group["interval"] * 1000)
                self.assertIn(group["max-failed-times"], (1, 2))
                self.assertEqual(group["expected-status"], 200)
                self.assertEqual(group["url"], "https://www.apple.com/library/test/success.html")
                if group["type"] == "url-test":
                    self.assertGreaterEqual(group["tolerance"], 50)
                else:
                    self.assertNotIn("tolerance", group)
                if "filter" in group:
                    self.assertEqual(group["empty-fallback"], "REJECT")
                else:
                    # A one-child automatic group is valid, but adds no backup.
                    self.assertGreaterEqual(len(group["proxies"]), 1)

    def test_selectors_do_not_schedule_redundant_health_checks(self):
        for group in self.groups.values():
            if group["type"] == "select":
                with self.subTest(group=group["name"]):
                    self.assertEqual(group.get("interval", 0), 0)
                    self.assertNotIn("max-failed-times", group)
                    self.assertNotIn("lazy", group)
                    self.assertNotIn("tolerance", group)

    def test_download_route_has_explicit_final_fallback(self):
        maximum = self.groups["⬇️.Line-[Relay.VPS]-Max.Traffic"]
        download = self.groups["⬇️🔰.DirectExit-[Download]"]
        self.assertEqual(maximum["proxies"], [download["name"], "♾️.Line-[Final]"])
        self.assertEqual(download["type"], "url-test")
        self.assertFalse(download.get("proxies"))
        self.assertFalse(download.get("use"))
        self.assertTrue(download["include-all"])
        self.assertEqual(download["exclude-filter"], r"(?i)PrxChain")

        def included(name):
            return bool(re.search(download["filter"], name)) and not bool(
                re.search(download["exclude-filter"], name)
            )

        for index in range(4):
            self.assertTrue(included(node_name("jp", "vless", index, allow_download=True)))
        for role, direct, allowed, showip in (
            ("Exit", True, False, False),
            ("Exit", False, True, False),
            ("Exit", True, True, True),
            ("HomeIP", True, True, False),
        ):
            name = node_name("jp", "vless", 0, role, allow_direct_exit=direct,
                             allow_download=allowed, allow_showip=showip)
            with self.subTest(node=name):
                self.assertEqual(included(name), allowed)
        self.assertFalse(included("PrxChain-[JP]-example-[Download=true]"))
        self.assertFalse(included("VPS-[JP.Exit]-VLESS-00-(PrxChain)-[Download=true]"))

    def test_cdn_keeps_manual_choices_without_an_extra_failover_layer(self):
        business = self.groups["☁️.<Global>--CDN"]
        self.assertEqual(business["type"], "select")
        self.assertNotIn("☁️.Line-[CDN]", self.groups)
        self.assertEqual(business["proxies"][:2], [
            "⬇️.Line-[Relay.VPS]-Max.Traffic",
            "⚡.Line-[Relay.VPS]-Low.Latency",
        ])
        self.assertIn("DIRECT", business["proxies"])

    def test_regional_failover_preserves_exit_region_and_role(self):
        regional = [g for g in self.groups.values() if g["type"] == "fallback"]
        self.assertTrue(regional)
        pair_groups = [
            group for group in regional
            if len(group.get("proxies", [])) == 2
            and any(".Chain-" in proxy for proxy in group["proxies"])
            and any(".DirectExit-" in proxy for proxy in group["proxies"])
        ]
        self.assertTrue(pair_groups)
        for group in pair_groups:
            with self.subTest(group=group["name"]):
                suffix = group["name"].split(".Line-", 1)[1]
                self.assertTrue(
                    any(proxy.endswith(".Chain-" + suffix) for proxy in group["proxies"])
                )
                self.assertTrue(
                    any(proxy.endswith(".DirectExit-" + suffix) for proxy in group["proxies"])
                )

    def test_selected_regional_lines_prefer_chain_and_match_home_filters(self):
        regions = {"US": "🇺🇸", "JP": "🇯🇵", "SG": "🇸🇬"}
        for region, flag in regions.items():
            with self.subTest(region=region):
                line = self.groups[f"{flag}.Line-[{region}]"]
                self.assertEqual(
                    line["proxies"],
                    [
                        f"{flag}🔗.Chain-[{region}]",
                        f"{flag}🔰.DirectExit-[{region}]",
                    ],
                )

                exit_proxy = {
                    "name": node_name(region.lower(), "vless", 0, "Exit"),
                    "_allow-direct-exit": True,
                    "_allow-chain-exit": True,
                }
                dialer = {
                    "name": node_name("us", "vless", 1, "Core"),
                    "_allow-relay": True,
                }
                generator.validate_generated_against_home(
                    [exit_proxy, dialer], [(exit_proxy, dialer)]
                )

        for region, flag in {"HK": "🇭🇰", "MY": "🇲🇾"}.items():
            with self.subTest(hidden_chain_region=region):
                chain = self.groups[f"{flag}🔗.Chain-[{region}]"]
                self.assertTrue(chain["hidden"])


if __name__ == "__main__":
    unittest.main()
