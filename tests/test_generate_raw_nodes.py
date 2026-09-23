import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import generate_raw_nodes as generator


class GeneratorTests(unittest.TestCase):
    def test_same_region_general_chain_can_use_a_distinct_landing(self) -> None:
        dialer = {
            "name": generator.node_name("us", "vless", 0, "Core"),
            "type": "vless",
            "_allow-relay": True,
            "_relay-protocol": "vless",
            "_physical-node-id": "vps-us-relay",
        }
        exit_proxy = {
            "name": generator.node_name("us", "vless", 1, "Exit"),
            "type": "vless",
            "_allow-chain-exit": True,
            "_chain-exit-protocol": "vless",
            "_physical-node-id": "vps-us-landing",
        }

        candidates = generator.chain_candidates([dialer, exit_proxy])

        self.assertEqual(candidates, [(exit_proxy, dialer)])
        self.assertEqual(generator.route_key(candidates[0]), ("US", "US"))
        home = generator.load_yaml(generator.SCRIPT_DIR / "home.yaml")
        chain_group = next(
            group for group in home["proxy-groups"] if group["name"] == "🇺🇸🔗.Chain-[US]"
        )
        self.assertRegex(generator.chain_name(*candidates[0]), chain_group["filter"])

    def test_same_region_showip_general_chain_can_use_a_distinct_landing(self) -> None:
        dialer = {
            "name": generator.node_name("us", "vless", 0, "Core"),
            "type": "vless",
            "_allow-relay": True,
            "_relay-protocol": "vless",
            "_physical-node-id": "vps-us-relay",
        }
        showip_exit = {
            "name": generator.node_name("us", "vless", 1, "Exit", allow_showip=True),
            "type": "vless",
            "_allow-chain-exit": True,
            "_allow-showip": True,
            "_chain-exit-protocol": "vless",
            "_physical-node-id": "vps-us-showip",
        }

        candidates = generator.chain_candidates([dialer, showip_exit])

        self.assertEqual(candidates, [(showip_exit, dialer)])
        self.assertEqual(generator.route_key(candidates[0]), ("US", "US"))
        self.assertTrue(generator.chain_name(*candidates[0]).endswith("[ShowIP=true]"))

    def test_homeip_route_is_case_insensitive(self) -> None:
        dialer = {
            "name": generator.node_name("jp", "vless", 0, "Core"),
            "type": "vless",
            "_allow-relay": True,
            "_relay-protocol": "vless",
            "_physical-node-id": "vps-jp",
        }
        exit_proxy = {
            "name": generator.node_name("us", "vless", 0, "HomeIP"),
            "type": "vless",
            "_allow-chain-exit": True,
            "_chain-exit-protocol": "vless",
            "_physical-node-id": "vps-us-homeip",
        }

        selected = generator.select_routes(
            [dialer, exit_proxy],
            "us.homeip<-JP",
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(generator.route_key(selected[0]), ("US.HomeIP", "JP"))

    def test_direct_exit_capability_is_encoded_in_node_name_only(self) -> None:
        name = generator.node_name(
            "us",
            "vless",
            0,
            "Core",
            allow_direct_exit=False,
        )

        self.assertIn("[Direct=false]", name)
        self.assertEqual(generator.node_meta(name)["direct"], "false")

    def test_airport_nodes_share_anchor_counter_with_self_hosted_nodes(self) -> None:
        base = {
            "name": generator.node_name("us", "vless", 0, "Exit"),
            "type": "vless",
            "server": "self-hosted.example",
            "port": 443,
            "uuid": "self-hosted-uuid",
            "tls": True,
        }
        counters = {("us", "vless"): 1}
        airport = generator.normalize_airport_nodes(
            [
                {
                    "name": "Example Airport US",
                    "type": "vless",
                    "server": "airport.example",
                    "port": 443,
                    "uuid": "airport-uuid",
                }
            ],
            counters,
        )

        self.assertEqual(generator.node_meta(airport[0]["name"])["idx"], "01")
        self.assertNotIn("[ShowIP=true]", airport[0]["name"])
        self.assertNotEqual(
            generator.anchor_name(base["name"]),
            generator.anchor_name(airport[0]["name"]),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "template.yaml"
            generator.write_template([base, airport[0]], [], output)
            parsed = yaml.safe_load(output.read_text())
            self.assertEqual(len(parsed["proxies"]), 2)

    def test_airport_hysteria2_uses_h2_protocol_label(self) -> None:
        airport = generator.normalize_airport_nodes(
            [
                {
                    "name": "Example Airport US",
                    "type": "hysteria2",
                    "server": "airport.example",
                    "port": 443,
                    "password": "password",
                }
            ]
        )

        self.assertEqual(generator.node_meta(airport[0]["name"])["proto"], "H2")
        self.assertNotIn("HYSTERIA2", airport[0]["name"])

    def test_trusted_nodes_are_loaded_with_explicit_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "trusted-nodes.yaml"
            source.write_text(
                yaml.safe_dump(
                    {
                        "nodes": [
                            {
                                "id": "trusted-us-01",
                                "name": "US NAT VLESS 01",
                                "region": "US",
                                "allow-relay": False,
                                "allow-chain-exit": False,
                                "allow-direct-exit": True,
                                "proxy": {
                                    "type": "vless",
                                    "server": "trusted.example",
                                    "port": 443,
                                    "uuid": "trusted-uuid",
                                },
                            }
                        ]
                    }
                )
            )

            nodes = generator.load_trusted_nodes(source, {})

        self.assertEqual(len(nodes), 1)
        meta = generator.node_meta(nodes[0]["name"])
        self.assertEqual(meta["region"], "US")
        self.assertEqual(meta["role"], "Exit")
        self.assertEqual(meta["proto"], "VLESS")
        self.assertIsNone(meta["airport"])
        self.assertIsNone(meta["trusted"])
        self.assertEqual(nodes[0]["name"], "VPS-[US.Exit]-VLESS-00-(美国出口节点)")
        self.assertFalse(nodes[0]["_allow-relay"])
        self.assertTrue(nodes[0]["_allow-direct-exit"])
        self.assertFalse(nodes[0]["_allow-chain-exit"])
        self.assertFalse(nodes[0]["_allow-download"])
        self.assertFalse(nodes[0]["_allow-showip"])

    def test_trusted_showip_capability_is_encoded_and_matches_home_filters(self) -> None:
        nodes = generator.normalize_trusted_nodes(
            [
                {
                    "id": "trusted-us-showip",
                    "region": "US",
                    "allow-chain-exit": True,
                    "allow-showip": True,
                    "proxy": {
                        "type": "vless",
                        "server": "trusted.example",
                        "port": 443,
                        "uuid": "trusted-uuid",
                    },
                }
            ],
            {},
            Path("trusted-nodes.yaml"),
        )

        node = nodes[0]
        self.assertTrue(node["_allow-showip"])
        self.assertTrue(node["_allow-chain-exit"])
        self.assertIn("[ShowIP=true]", node["name"])

        home = generator.load_yaml(generator.SCRIPT_DIR / "home.yaml")
        direct_group = next(
            group
            for group in home["proxy-groups"]
            if group["name"] == "🇺🇸🔰.DirectExit-[US.ShowIP]"
        )
        self.assertRegex(node["name"], direct_group["filter"])
        self.assertIsNone(re.search(direct_group["exclude-filter"], node["name"]))

        dialer = {"name": generator.node_name("jp", "vless", 0, "Core")}
        chain = generator.chain_name(node, dialer)
        chain_group = next(
            group
            for group in home["proxy-groups"]
            if group["name"] == "🇺🇸🔗.Chain-[US.ShowIP]"
        )
        self.assertRegex(chain, chain_group["filter"])

    def test_trusted_showip_flag_rejects_invalid_types(self) -> None:
        base = {
            "id": "trusted-us-showip",
            "region": "US",
            "proxy": {
                "type": "vless",
                "server": "trusted.example",
                "port": 443,
                "uuid": "trusted-uuid",
            },
        }
        for value in ("maybe", 2, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "allow-showip"
            ):
                generator.normalize_trusted_nodes(
                    [{**base, "allow-showip": value}],
                    {},
                    Path("trusted-nodes.yaml"),
                )

    def test_generated_names_match_current_home_filters(self) -> None:
        exit_proxy = {
            "name": generator.node_name(
                "us", "vless", 0, "HomeIP", allow_showip=True, allow_download=True
            ),
            "_allow-direct-exit": True,
            "_allow-chain-exit": True,
            "_allow-showip": True,
            "_allow-download": True,
            "_chain-exit-protocol": "vless",
            "_physical-node-id": "vps-us-homeip",
        }
        dialer = {
            "name": generator.node_name("jp", "vless", 0, "Core"),
            "_allow-relay": True,
            "_relay-protocol": "vless",
            "_physical-node-id": "vps-jp-core",
        }
        generator.validate_generated_against_home(
            [exit_proxy, dialer], [(exit_proxy, dialer)]
        )

    def test_showip_without_direct_exit_can_match_showip_chain_filter(self) -> None:
        proxy = {
            "name": generator.node_name(
                "us",
                "socks5",
                0,
                "Exit",
                allow_direct_exit=False,
                allow_showip=True,
            ),
            "type": "socks5",
            "_allow-direct-exit": False,
            "_allow-showip": True,
            "_allow-chain-exit": True,
            "_chain-exit-protocol": "socks5",
            "_allow-relay": False,
            "_physical-node-id": "trusted:us-showip",
        }
        dialer = {
            "name": generator.node_name("us", "vless", 0, "Core"),
            "type": "vless",
            "_allow-direct-exit": True,
            "_allow-chain-exit": False,
            "_allow-relay": True,
            "_relay-protocol": "vless",
            "_physical-node-id": "vps-us-relay",
        }
        chains = generator.chain_candidates([proxy, dialer])
        self.assertEqual(chains, [(proxy, dialer)])

        home = generator.load_yaml(generator.HOME_TEMPLATE)
        showip_group = next(
            group
            for group in home["proxy-groups"]
            if group["name"] == "🇺🇸🔰.DirectExit-[US.ShowIP]"
        )
        showip_chain_group = next(
            group
            for group in home["proxy-groups"]
            if group["name"] == "🇺🇸🔗.Chain-[US.ShowIP]"
        )
        chain_name = generator.chain_name(*chains[0])

        self.assertFalse(generator._group_matches_proxy(showip_group, proxy["name"]))
        self.assertTrue(generator._group_matches_proxy(showip_chain_group, chain_name))
        generator.validate_generated_against_home([proxy, dialer], chains)

    def test_generated_chain_is_rejected_when_home_filter_changes(self) -> None:
        exit_proxy = {
            "name": generator.node_name("us", "vless", 0, "Exit"),
            "_allow-chain-exit": True,
            "_chain-exit-protocol": "vless",
            "_physical-node-id": "vps-us-exit",
        }
        dialer = {
            "name": generator.node_name("jp", "vless", 0, "Core"),
            "_allow-relay": True,
            "_relay-protocol": "vless",
            "_physical-node-id": "vps-jp-core",
        }
        with tempfile.TemporaryDirectory() as directory:
            template = Path(directory) / "home.yaml"
            source = generator.load_yaml(generator.HOME_TEMPLATE)
            group = next(
                group
                for group in source["proxy-groups"]
                if group["name"].endswith(".Chain-[US]")
            )
            group["filter"] = r"(?i)^NO_MATCH$"
            template.write_text(
                yaml.safe_dump(source, allow_unicode=True), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "未命中 home 模板筛选"):
                generator.validate_generated_against_home(
                    [exit_proxy, dialer], [(exit_proxy, dialer)], template
                )

    def test_home_template_failure_keeps_existing_template_output(self) -> None:
        proxy = {
            "name": generator.node_name("us", "vless", 0, "Exit"),
            "type": "vless",
            "server": "edge.example",
            "port": 443,
            "uuid": "uuid",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "home.yaml"
            source = generator.load_yaml(generator.HOME_TEMPLATE)
            group = next(
                group
                for group in source["proxy-groups"]
                if group["name"].endswith(".DirectExit-[US]")
            )
            group["filter"] = r"(?i)^NO_MATCH$"
            template.write_text(
                yaml.safe_dump(source, allow_unicode=True), encoding="utf-8"
            )
            output = root / "generated.yaml"
            output.write_text("old output\n", encoding="utf-8")
            with (
                mock.patch.object(generator, "collect_proxies", return_value=[proxy]),
                mock.patch.object(generator, "load_trusted_nodes", return_value=[]),
                self.assertRaisesRegex(SystemExit, "home 模板"),
            ):
                generator.main(
                    [
                        "--template",
                        "--no-loon",
                        "--home-template",
                        str(template),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(output.read_text(encoding="utf-8"), "old output\n")

    def test_trusted_homeip_uses_self_hosted_role_and_constraints(self) -> None:
        source = {
            "id": "trusted-us-homeip",
            "region": "US",
            "exit-type": "homeip",
            "allow-chain-exit": True,
            "allow-showip": True,
            "proxy": {
                "type": "vless",
                "server": "trusted.example",
                "port": 443,
                "uuid": "trusted-uuid",
            },
        }
        node = generator.normalize_trusted_nodes(
            [source], {}, Path("trusted-nodes.yaml")
        )[0]

        self.assertEqual(generator.node_meta(node["name"])["role"], "HomeIP")
        self.assertIn("[ShowIP=true]", node["name"])

        home = generator.load_yaml(generator.SCRIPT_DIR / "home.yaml")
        homeip_group = next(
            group
            for group in home["proxy-groups"]
            if group["name"] == "🇺🇸🔰.DirectExit-[US.HomeIP]"
        )
        showip_group = next(
            group
            for group in home["proxy-groups"]
            if group["name"] == "🇺🇸🔰.DirectExit-[US.ShowIP]"
        )
        self.assertRegex(node["name"], homeip_group["filter"])
        self.assertRegex(node["name"], showip_group["filter"])

        with self.assertRaisesRegex(ValueError, "allow-relay"):
            generator.normalize_trusted_nodes(
                [{**source, "allow-relay": True}],
                {},
                Path("trusted-nodes.yaml"),
            )

    def test_trusted_socks5_is_supported_with_authentication(self) -> None:
        nodes = generator.normalize_trusted_nodes(
            [
                {
                    "id": "trusted-us-01",
                    "region": "US",
                    "proxy": {
                        "type": "socks5",
                        "server": "nat.example",
                        "port": 1080,
                        "username": "nat-user",
                        "password": "nat-password",
                        "udp": True,
                    },
                }
            ],
            {},
            Path("trusted-nodes.yaml"),
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nodes.yaml"
            generator.write_template(nodes, [], output)
            rendered = yaml.safe_load(output.read_text())["proxies"][0]

        self.assertEqual(rendered["type"], "socks5")
        self.assertEqual(rendered["name"], "VPS-[US.Exit]-SOCKS5-00-(美国出口节点)")
        self.assertEqual(rendered["username"], "nat-user")
        self.assertFalse(nodes[0]["_allow-relay"])
        self.assertFalse(nodes[0]["_allow-chain-exit"])

    def test_trusted_socks5_rejects_anonymous_authentication(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能使用匿名认证"):
            generator.normalize_trusted_nodes(
                [
                    {
                        "id": "trusted-us-01",
                        "region": "US",
                        "proxy": {
                            "type": "socks5",
                            "server": "nat.example",
                            "port": 1080,
                        },
                    }
                ],
                {},
                Path("trusted-nodes.yaml"),
            )

    def test_direct_source_nodes_reject_non_mapping_and_chains(self) -> None:
        options = {
            "source_marker": "Trusted",
            "description_suffix": "NAT机",
            "physical_source": "trusted",
            "source_kind": "可信 NAT",
        }
        with self.assertRaisesRegex(ValueError, "必须是映射"):
            generator.normalize_direct_source_nodes(["not-a-mapping"], {}, **options)

        chained = {
            "name": "Example US",
            "type": "vless",
            "server": "trusted.example",
            "port": 443,
            "dialer-proxy": "some-upstream",
        }
        with self.assertRaisesRegex(ValueError, "dialer-proxy"):
            generator.normalize_direct_source_nodes([chained], {}, **options)

    def test_trusted_file_rejects_false_list_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "trusted-nodes.yaml"
            source.write_text("nodes: false\n")
            with self.assertRaisesRegex(ValueError, "nodes 必须是列表"):
                generator.load_trusted_nodes(source, {})

            source.write_text("false\n")
            with self.assertRaisesRegex(ValueError, "顶层必须是映射"):
                generator.load_trusted_nodes(source, {})

    def test_trusted_nodes_use_host_var_naming(self) -> None:
        nodes = generator.normalize_trusted_nodes(
            [
                {
                    "id": "trusted-us",
                    "name": "VPS-[US.Core]-source",
                    "region": "US",
                    "proxy": {
                        "type": "vless",
                        "server": "trusted.example",
                        "port": 443,
                        "uuid": "trusted-uuid",
                    },
                }
            ],
            {},
            Path("trusted-nodes.yaml"),
        )

        self.assertEqual(nodes[0]["name"], "VPS-[US.Exit]-VLESS-00-(美国出口节点)")
        self.assertIsNone(generator.node_meta(nodes[0]["name"])["trusted"])

    def test_trusted_nodes_require_scalar_identity_server_and_credentials(self) -> None:
        invalid_nodes = (
            (
                {
                    "id": None,
                    "region": "US",
                    "proxy": {
                        "type": "vless",
                        "server": "trusted.example",
                        "port": 443,
                        "uuid": "uuid",
                    },
                },
                "id",
            ),
            (
                {
                    "id": "trusted-us",
                    "region": "US",
                    "proxy": {
                        "type": "vless",
                        "server": {"host": "trusted.example"},
                        "port": 443,
                        "uuid": "uuid",
                    },
                },
                "server",
            ),
            (
                {
                    "id": "trusted-us",
                    "region": "US",
                    "proxy": {
                        "type": "vless",
                        "server": "trusted.example",
                        "port": 443,
                    },
                },
                "uuid",
            ),
            (
                {
                    "id": "trusted-us",
                    "region": "US",
                    "proxy": {
                        "type": "hysteria2",
                        "server": "trusted.example",
                        "port": 443,
                    },
                },
                "password",
            ),
        )
        for source, message in invalid_nodes:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    generator.normalize_trusted_nodes(
                        [source],
                        {},
                        Path("trusted-nodes.yaml"),
                    )

    def test_trusted_nodes_show_trust_file_as_interactive_source(self) -> None:
        proxy = {
            "name": "VPS-[US.Exit]-VLESS-00-(美国出口节点)",
            "_physical-node-id": "trusted:trusted-us",
        }

        self.assertEqual(
            generator.interactive_proxy_label(proxy),
            "VPS-[US.Exit]-VLESS-00-(美国出口节点)（来源文件: trusted-nodes.yaml）",
        )

    def test_direct_source_region_rejects_virtual_codes(self) -> None:
        self.assertIsNone(
            generator.airport_region("VPS-[EUR.Core]-VLESS-00-(欧洲核心节点)")
        )
        self.assertIsNone(generator.airport_region("Example XX"))
        with self.assertRaisesRegex(ValueError, "两位国家代码"):
            generator.normalize_trusted_nodes(
                [
                    {
                        "id": "unknown",
                        "region": "XX",
                        "proxy": {
                            "type": "vless",
                            "server": "trusted.example",
                            "port": 443,
                        },
                    }
                ],
                {},
                Path("trusted-nodes.yaml"),
            )

    def test_trusted_file_rejects_proxies_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "trusted-nodes.yaml"
            source.write_text("proxies: []\n")

            with self.assertRaisesRegex(ValueError, "只支持 nodes"):
                generator.load_trusted_nodes(source, {})

    def test_duplicate_anchor_is_rejected(self) -> None:
        name = generator.node_name("us", "vless", 0, "Exit")
        with self.assertRaisesRegex(ValueError, "anchor 重复"):
            generator.ensure_unique_anchors([{"name": name}, {"name": name}])

    def test_template_quotes_imported_mapping_keys(self) -> None:
        injected_key = "x : 1 }\ninjected: true #"
        proxy = {
            "name": generator.node_name("us", "vless", 0, "Exit"),
            "type": "vless",
            "server": "edge.example",
            "port": 443,
            "uuid": "uuid",
            injected_key: "kept-as-data",
            "ws-opts": {"headers": {injected_key: "nested-data"}},
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "template.yaml"
            generator.write_template([proxy], [], output)
            parsed = yaml.safe_load(output.read_text())

        self.assertNotIn("injected", parsed)
        self.assertEqual(parsed["proxies"][0][injected_key], "kept-as-data")
        self.assertEqual(
            parsed["proxies"][0]["ws-opts"]["headers"][injected_key],
            "nested-data",
        )

    def test_outputs_reject_non_string_proxy_keys_cleanly(self) -> None:
        proxy = {
            "name": generator.node_name("us", "vless", 0, "Exit"),
            "type": "vless",
            "server": "edge.example",
            "port": 443,
            "uuid": "uuid",
            1: "invalid-key",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for writer, filename in (
                (lambda path: generator.write_template([proxy], [], path), "template.yaml"),
                (lambda path: generator.write_plain([proxy], path), "plain.yaml"),
            ):
                with self.subTest(filename=filename):
                    with self.assertRaisesRegex(ValueError, "字段名必须是字符串"):
                        writer(root / filename)

    def test_reality_flow_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host_dir = Path(directory) / "vps-us"
            source = host_dir / "secrets" / "xray-inbounds.json"
            source.parent.mkdir(parents=True)
            source.write_text(
                json.dumps(
                    {
                        "inbounds": [
                            {
                                "protocol": "vless",
                                "port": 443,
                                "settings": {
                                    "clients": [
                                        {
                                            "id": "reality-uuid",
                                            "flow": "xtls-rprx-vision",
                                            "encryption": "none",
                                        }
                                    ]
                                },
                                "streamSettings": {
                                    "network": "tcp",
                                    "security": "reality",
                                    "realitySettings": {
                                        "serverNames": ["edge.example"],
                                        "publicKey": "public-key",
                                        "shortIds": ["01"],
                                        "fingerprint": "firefox",
                                    },
                                },
                            }
                        ]
                    }
                )
            )

            nodes = generator.xray_nodes(
                host_dir,
                {"VPS_CLASH_REGION": "us", "VPS_HOST": "edge.example"},
                {},
            )

        self.assertEqual(nodes[0]["flow"], "xtls-rprx-vision")
        self.assertEqual(nodes[0]["reality-opts"]["public-key"], "public-key")

    def test_tls_xray_node_requires_explicit_connection_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host_dir = Path(directory) / "vps-us"
            source = host_dir / "secrets" / "xray-inbounds.json"
            source.parent.mkdir(parents=True)
            source.write_text(
                json.dumps(
                    {
                        "inbounds": [
                            {
                                "protocol": "vless",
                                "port": 443,
                                "settings": {"clients": [{"id": "uuid"}]},
                                "streamSettings": {
                                    "security": "tls",
                                    "tlsSettings": {},
                                },
                            }
                        ]
                    }
                )
            )

            with self.assertRaisesRegex(ValueError, "VPS_HOST"):
                generator.xray_nodes(
                    host_dir,
                    {"VPS_CLASH_REGION": "us"},
                    {},
                )

    def test_invalid_xray_json_is_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host_dir = Path(directory) / "vps-us"
            source = host_dir / "secrets" / "xray-inbounds.json"
            source.parent.mkdir(parents=True)
            source.write_text("not-json")

            with self.assertRaisesRegex(ValueError, "有效的 Xray JSON"):
                generator.xray_nodes(
                    host_dir,
                    {"VPS_CLASH_REGION": "us"},
                    {},
                )

    def test_client_inventory_is_authoritative_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host_dir = Path(directory) / "vps-nat"
            inventory = host_dir / "client" / "clash-nodes.yaml"
            inventory.parent.mkdir(parents=True)
            env = {"VPS_CLASH_REGION": "jp"}

            inventory.write_text("- not-a-mapping\n")
            with self.assertRaisesRegex(ValueError, "顶层必须是映射"):
                generator.client_inventory_nodes(host_dir, env, {})

            inventory.write_text(
                yaml.safe_dump(
                    {
                        "proxies": [
                            {
                                "type": "vless",
                                "server": "public.example",
                                "port": 70000,
                                "uuid": "uuid",
                            }
                        ]
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "1-65535"):
                generator.client_inventory_nodes(host_dir, env, {})

            inventory.write_text("proxies: false\n")
            with self.assertRaisesRegex(ValueError, "proxies 必须是列表"):
                generator.client_inventory_nodes(host_dir, env, {})

            inventory.write_text("proxies: []\n")
            self.assertEqual(generator.client_inventory_nodes(host_dir, env, {}), [])

    def test_client_inventory_rejects_chains_and_missing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host_dir = Path(directory) / "vps-nat"
            inventory = host_dir / "client" / "clash-nodes.yaml"
            inventory.parent.mkdir(parents=True)
            env = {"VPS_CLASH_REGION": "jp"}

            inventory.write_text(
                yaml.safe_dump(
                    {
                        "proxies": [
                            {
                                "type": "vless",
                                "server": "public.example",
                                "port": 443,
                                "uuid": "uuid",
                                "dialer-proxy": "already-chained",
                            }
                        ]
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "dialer-proxy"):
                generator.client_inventory_nodes(host_dir, env, {})

            inventory.write_text(
                yaml.safe_dump(
                    {
                        "proxies": [
                            {
                                "type": "hysteria2",
                                "server": "public.example",
                                "port": 443,
                            }
                        ]
                    }
                )
            )
            with self.assertRaisesRegex(ValueError, "password"):
                generator.client_inventory_nodes(host_dir, env, {})

    def test_client_inventory_accepts_authenticated_socks5(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            host_dir = Path(directory) / "vps-nat"
            inventory = host_dir / "client" / "clash-nodes.yaml"
            inventory.parent.mkdir(parents=True)
            inventory.write_text(
                yaml.safe_dump(
                    {
                        "proxies": [
                            {
                                "type": "socks5",
                                "server": "public.example",
                                "port": 1080,
                                "username": "nat-user",
                                "password": "nat-password",
                            }
                        ]
                    }
                )
            )

            nodes = generator.client_inventory_nodes(
                host_dir,
                {"VPS_CLASH_REGION": "us"},
                {},
            )

        self.assertEqual(nodes[0]["type"], "socks5")
        self.assertEqual(nodes[0]["name"], "VPS-[US.Exit]-SOCKS5-00-(美国出口节点)")

    def test_default_hosts_dir_requires_an_active_host_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            preferred = home / ".config" / "infra" / "hosts"
            legacy = home / "servers" / "hosts"
            (preferred / "vps-retired").mkdir(parents=True)
            active = legacy / "vps-active"
            active.mkdir(parents=True)
            (active / "host.env").write_text("VPS_CLASH_REGION=us\n")

            with (
                mock.patch.object(generator.Path, "home", return_value=home),
                mock.patch.object(generator, "SCRIPT_DIR", root / "repo"),
            ):
                self.assertEqual(generator.default_hosts_dir(), legacy)

    def test_collect_proxies_ignores_vps_template_before_sorting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hosts_dir = Path(directory)
            template = hosts_dir / "vps-template"
            template.mkdir()
            (template / "host.env").write_text("VPS_CLASH_ORDER=invalid\n")

            self.assertEqual(generator.collect_proxies(hosts_dir), [])

    def test_invalid_host_order_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hosts_dir = Path(directory)
            host_dir = hosts_dir / "vps-one"
            host_dir.mkdir()
            (host_dir / "host.env").write_text(
                "VPS_CLASH_REGION=jp\nVPS_CLASH_ORDER=not-an-integer\n"
            )

            with self.assertRaisesRegex(ValueError, "VPS_CLASH_ORDER 必须是整数"):
                generator.collect_proxies(hosts_dir)

    def test_host_region_must_be_an_actual_two_letter_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "VPS_CLASH_REGION"):
            generator.host_capabilities(Path("vps-missing-region"), {})
        with self.assertRaisesRegex(ValueError, "VPS_CLASH_REGION"):
            generator.host_capabilities(
                Path("vps-virtual-region"), {"VPS_CLASH_REGION": "EUR"}
            )

    def test_listen_port_supports_single_ports_and_ranges(self) -> None:
        self.assertEqual(generator.port_from_listen(":443", 20002), 443)
        self.assertEqual(generator.port_from_listen(":443-500", 20002), 443)
        self.assertEqual(generator.port_from_listen("443", 20002), 443)
        with self.assertRaisesRegex(ValueError, "listen"):
            generator.port_from_listen("definitely-invalid", 20002)

    def test_loon_vless_uses_supported_common_options(self) -> None:
        body = generator.loon_node_body(
            {
                "type": "vless",
                "server": "edge.example",
                "port": 443,
                "uuid": "uuid",
                "flow": "xtls-rprx-vision",
                "tls": True,
                "udp": True,
                "servername": "edge.example",
                "reality-opts": {"public-key": "public-key", "short-id": "01"},
            }
        )

        self.assertIn("flow=xtls-rprx-vision", body)
        self.assertIn("public-key=\"public-key\"", body)
        self.assertNotIn("client-fingerprint", body)
        self.assertNotIn("block-quic", body)

    def test_loon_socks5_preserves_authentication_and_supported_options(self) -> None:
        password = ' pass "测试 '
        body = generator.loon_node_body(
            {
                "type": "socks5",
                "server": "nat.example",
                "port": 1080,
                "username": "user,name",
                "password": password,
                "tls": True,
                "servername": "proxy.example",
                "skip-cert-verify": False,
                "udp": False,
            }
        )

        self.assertTrue(body.startswith('socks5,nat.example,1080,"user,name",'))
        self.assertIn(generator.loon_quote(password), body)
        self.assertIn("over-tls=true", body)
        self.assertIn("sni=proxy.example", body)
        self.assertIn("skip-cert-verify=false", body)
        self.assertIn("udp=false", body)

    def test_loon_writes_authenticated_socks5_without_skipping_it(self) -> None:
        proxy = {
            "name": generator.node_name("us", "socks5", 0, "Exit"),
            "type": "socks5",
            "server": "nat.example",
            "port": 1080,
            "username": "nat-user",
            "password": "nat-password",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nodes.conf"
            count, skipped = generator.write_loon([proxy], output)
            content = output.read_text()

        self.assertEqual(count, 1)
        self.assertEqual(skipped, [])
        self.assertIn('socks5,nat.example,1080,nat-user,"nat-password"', content)

    def test_loon_socks5_requires_authentication(self) -> None:
        with self.assertRaisesRegex(ValueError, "SOCKS5.*username"):
            generator.loon_node_body(
                {
                    "type": "socks5",
                    "server": "nat.example",
                    "port": 1080,
                    "password": "password",
                }
            )

    def test_loon_socks5_rejects_missing_null_invalid_and_nested_fields(self) -> None:
        base = {
            "type": "socks5",
            "server": "nat.example",
            "port": 1080,
            "username": "user",
            "password": "password",
        }
        for extra in (
            {"password": None},
            {"tls": []},
            {"smux": {"enabled": False}},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                generator.loon_node_body(base | extra)

    def test_loon_skips_non_scalar_credentials_without_crashing(self) -> None:
        proxy = {
            "name": generator.node_name("us", "hysteria2", 0, "Exit"),
            "type": "hysteria2",
            "server": "edge.example",
            "port": 443,
            "password": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nodes.conf"
            count, skipped = generator.write_loon([proxy], output)

        self.assertEqual(count, 0)
        self.assertEqual(len(skipped), 1)
        self.assertIn("password", skipped[0])

    def test_number_prompt_exits_cleanly_on_eof(self) -> None:
        with mock.patch("builtins.input", side_effect=EOFError):
            with self.assertRaisesRegex(SystemExit, "输入已结束"):
                generator.prompt_number_selection(3)

    def test_output_path_collision_is_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "输出路径冲突"):
            generator.validate_output_paths(
                [("主输出", Path("output.yaml")), ("raw-output", Path("./output.yaml"))]
            )

    def test_conflicting_cli_modes_are_rejected(self) -> None:
        combinations = (
            ["--plain", "--routes", "US<-JP"],
            ["--routes", "US<-JP", "--chains", "none"],
            ["--interactive", "--exclude-node", "US"],
        )
        for arguments in combinations:
            with self.subTest(arguments=arguments):
                with mock.patch("sys.stderr", new=io.StringIO()):
                    with self.assertRaises(SystemExit):
                        generator.parse_args(arguments)

    def test_dns_policy_does_not_treat_hex_like_domain_as_ip(self) -> None:
        policy = generator.matching_airport_dns_policy(
            {"dns": {"nameserver-policy": {"+.abc.de": ["dns.example"]}}},
            [{"server": "node.abc.de"}],
        )

        self.assertEqual(policy, {"+.abc.de": ["dns.example"]})

    def test_secure_write_is_private_and_atomic_on_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nodes.yaml"
            generator.secure_write(output, "old\n")
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

            with mock.patch.object(generator.os, "replace", side_effect=OSError("boom")):
                with self.assertRaisesRegex(OSError, "boom"):
                    generator.secure_write(output, "new\n")

            self.assertEqual(output.read_text(), "old\n")
            self.assertEqual(list(Path(directory).glob(".nodes.yaml.*.tmp")), [])

    def test_main_reports_output_os_errors_without_traceback(self) -> None:
        proxy = {
            "name": generator.node_name("us", "vless", 0, "Exit"),
            "type": "vless",
            "server": "edge.example",
            "port": 443,
            "uuid": "uuid",
        }
        with (
            mock.patch.object(generator, "collect_proxies", return_value=[proxy]),
            mock.patch.object(generator, "load_trusted_nodes", return_value=[]),
            mock.patch.object(
                generator,
                "write_plain",
                side_effect=PermissionError("permission denied"),
            ),
        ):
            with self.assertRaisesRegex(SystemExit, "无法写入主输出"):
                generator.main(
                    ["--plain", "--no-loon", "--output", "/tmp/test-output.yaml"]
                )

    def test_interactive_template_contains_only_real_nodes(self) -> None:
        proxy = {
            "name": generator.node_name("us", "vless", 0, "Exit"),
            "type": "vless",
            "server": "edge.example",
            "port": 443,
            "uuid": "uuid",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "generated.yaml"
            with (
                mock.patch.object(generator, "collect_proxies", return_value=[proxy]),
                mock.patch.object(generator, "load_trusted_nodes", return_value=[]),
                mock.patch.object(generator, "interactive_airport_import", return_value=([], {})),
                mock.patch.object(
                    generator,
                    "interactive_selection",
                    return_value=([proxy], "template", []),
                ),
                mock.patch("builtins.input", side_effect=AssertionError("unexpected prompt")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                generator.main(
                    ["--interactive", "--no-loon", "--output", str(output)]
                )

            rendered = yaml.safe_load(output.read_text())

        self.assertEqual(rendered["proxies"], [proxy])


if __name__ == "__main__":
    unittest.main()
