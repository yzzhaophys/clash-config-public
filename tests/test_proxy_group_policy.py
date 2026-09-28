"""Guard the active Mihomo configuration's group graph and node selectors."""

import unittest

import generate_raw_nodes as generator
from node_io import load_yaml


class ProxyGroupPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_yaml(generator.MIHOMO_CONFIG)
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
        targets = set(self.groups) | {
            "DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"
        }
        for rule in self.config["rules"]:
            parts = [part.strip() for part in rule.split(",")]
            target = parts[-2] if parts[-1].lower() == "no-resolve" else parts[-1]
            with self.subTest(rule=rule):
                self.assertIn(target, targets)

    def test_automatic_groups_have_bounded_health_checks(self):
        automatic = [group for group in self.groups.values() if group["type"] != "select"]
        self.assertTrue(automatic)
        for group in automatic:
            with self.subTest(group=group["name"]):
                self.assertIn(group["type"], {"url-test", "fallback"})
                self.assertIs(group["lazy"], True)
                self.assertEqual(group["max-failed-times"], 2)
                self.assertEqual(group["expected-status"], 200)
                if group["type"] == "url-test":
                    self.assertEqual(group["interval"], 30)
                    self.assertEqual(group["timeout"], 3000)
                    self.assertEqual(group["tolerance"], 50)
                else:
                    self.assertIn(group["interval"], {45, 90})
                    self.assertIn(group["timeout"], {4000, 5000})

    def test_select_groups_do_not_schedule_health_checks(self):
        for group in self.groups.values():
            if group["type"] == "select":
                with self.subTest(group=group["name"]):
                    self.assertEqual(group.get("interval", 0), 0)
                    self.assertNotIn("max-failed-times", group)
                    self.assertNotIn("lazy", group)
                    self.assertNotIn("tolerance", group)

    def test_direct_exit_and_chain_filters_match_generated_names(self):
        flags = {
            "US": "🇺🇸", "JP": "🇯🇵", "SG": "🇸🇬", "HK": "🇭🇰",
            "MY": "🇲🇾", "TW": "🇹🇼", "AU": "🇦🇺", "UK": "🇬🇧",
            "DE": "🇩🇪", "NL": "🇳🇱", "FR": "🇫🇷",
        }
        for region, flag in flags.items():
            with self.subTest(region=region):
                direct_group = self.groups[f"{flag}.DirectExit-[{region}]"]
                chain_group = self.groups[f"{flag}.Chain-[{region}]"]

                for role in ("Core", "Exit"):
                    name = generator.node_name(region.lower(), "vless", 0, role)
                    self.assertTrue(generator._group_matches_proxy(direct_group, name))
                    no_direct = generator.node_name(
                        region.lower(), "vless", 1, role, allow_direct_exit=False
                    )
                    self.assertFalse(generator._group_matches_proxy(direct_group, no_direct))

                exit_proxy = {"name": generator.node_name(region.lower(), "vless", 0, "Exit")}
                dialer = {"name": generator.node_name("hk", "vless", 0, "Core")}
                chain = generator.chain_name(exit_proxy, dialer)
                self.assertTrue(generator._group_matches_proxy(chain_group, chain))

                if region in {"US", "JP", "SG"}:
                    homeip_group = self.groups[f"{flag}.DirectExit-[{region}.HomeIP]"]
                    homeip_chain_group = self.groups[f"{flag}.Chain-[{region}.HomeIP]"]
                    homeip = generator.node_name(region.lower(), "vless", 0, "HomeIP")
                    homeip_exit = {"name": generator.node_name(region.lower(), "vless", 1, "HomeIP")}
                    homeip_chain = generator.chain_name(homeip_exit, dialer)
                    self.assertTrue(generator._group_matches_proxy(homeip_group, homeip))
                    self.assertFalse(generator._group_matches_proxy(direct_group, homeip))
                    self.assertFalse(generator._group_matches_proxy(homeip_chain_group, chain))
                    self.assertTrue(generator._group_matches_proxy(homeip_chain_group, homeip_chain))

        self.assertFalse(any(
            "ShowIP" in name or "Download" in name for name in self.groups
        ))

    def test_regional_lines_use_configured_chain_and_direct_exit_order(self):
        flags = {
            "US": "🇺🇸", "JP": "🇯🇵", "SG": "🇸🇬", "HK": "🇭🇰",
            "MY": "🇲🇾", "TW": "🇹🇼", "AU": "🇦🇺", "UK": "🇬🇧",
            "DE": "🇩🇪", "NL": "🇳🇱", "FR": "🇫🇷",
        }
        for region, flag in flags.items():
            with self.subTest(region=region):
                line = self.groups[f"{flag}.Line-[{region}]"]
                chain = f"{flag}.Chain-[{region}]"
                direct = f"{flag}.DirectExit-[{region}]"
                expected = [chain, direct] if region in {"US", "JP", "SG"} else [direct, chain]
                self.assertEqual(line["proxies"], expected)

    def test_homeip_preferred_routes_fall_back_to_regular_region(self):
        for region, flag in {"US": "🇺🇸", "JP": "🇯🇵", "SG": "🇸🇬"}.items():
            name = f"🏠.Route-[{region}.HomeIP.Preferred]"
            with self.subTest(route=name):
                self.assertEqual(
                    self.groups[name]["proxies"],
                    [f"{flag}.Line-[{region}.HomeIP]", f"{flag}.Line-[{region}]"],
                )

    def test_region_routes_use_final_fallback_after_local_regions(self):
        expected = {
            "🌏.Route-[EastAsia]": [
                "🇯🇵.Line-[JP]", "🇭🇰.Line-[HK]", "🇹🇼.Line-[TW]",
                "♾️.Route-[Final.Fallback]",
            ],
            "🌏.Route-[SoutheastAsia]": [
                "🇸🇬.Line-[SG]", "🇲🇾.Line-[MY]", "♾️.Route-[Final.Fallback]",
            ],
            "🌎.Route-[Americas]": [
                "🇺🇸.Line-[US]", "♾️.Route-[Final.Fallback]",
            ],
            "🌏.Route-[Oceania]": [
                "🇦🇺.Line-[AU]", "♾️.Route-[Final.Fallback]",
            ],
            "🌍.Route-[Europe]": [
                "🇬🇧.Line-[UK]", "🇩🇪.Line-[DE]", "🇳🇱.Line-[NL]",
                "🇫🇷.Line-[FR]", "♾️.Route-[Final.Fallback]",
            ],
        }
        for name, members in expected.items():
            with self.subTest(route=name):
                self.assertEqual(self.groups[name]["proxies"], members)


if __name__ == "__main__":
    unittest.main()
