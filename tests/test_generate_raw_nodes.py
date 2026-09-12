import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import generate_raw_nodes as generator


class GeneratorTests(unittest.TestCase):
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

    def test_trusted_proxies_are_loaded_as_direct_nat_nodes(self) -> None:
        original_name = "VPS-[US.Core]-VLESS-00-(US核心节点)"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "trusted-nodes.yaml"
            source.write_text(
                yaml.safe_dump(
                    {
                        "proxies": [
                            {
                                "name": original_name,
                                "type": "vless",
                                "server": "trusted.example",
                                "port": 443,
                                "uuid": "trusted-uuid",
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
        self.assertIsNotNone(meta["trusted"])
        self.assertIn("(美国NAT机)", nodes[0]["name"])
        self.assertIn("[Trusted=VPS-［US.Core］-VLESS-00-(US核心节点)]", nodes[0]["name"])
        self.assertNotIn("VPS-[US.Core）", nodes[0]["name"])
        self.assertFalse(nodes[0]["_allow-relay"])
        self.assertTrue(nodes[0]["_allow-direct-exit"])
        self.assertFalse(nodes[0]["_allow-chain-exit"])
        self.assertFalse(nodes[0]["_allow-download"])

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
            for key in ("proxies", "nodes"):
                source.write_text(f"{key}: false\n")
                with self.subTest(key=key):
                    with self.assertRaisesRegex(ValueError, f"{key} 必须是列表"):
                        generator.load_trusted_nodes(source, {})

            source.write_text("false\n")
            with self.assertRaisesRegex(ValueError, "顶层必须是映射"):
                generator.load_trusted_nodes(source, {})

    def test_advanced_trusted_label_uses_balanced_fullwidth_brackets(self) -> None:
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
                    },
                }
            ],
            {},
            Path("trusted-nodes.yaml"),
        )

        self.assertIn("[Trusted=VPS-［US.Core］-source]", nodes[0]["name"])
        self.assertIsNotNone(generator.node_meta(nodes[0]["name"])["trusted"])

    def test_generated_region_must_be_two_letters_for_direct_sources(self) -> None:
        self.assertIsNone(
            generator.airport_region("VPS-[EUR.Core]-VLESS-00-(欧洲核心节点)")
        )

    def test_trusted_file_rejects_mixed_input_formats(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "trusted-nodes.yaml"
            source.write_text("proxies: []\nnodes: []\n")

            with self.assertRaisesRegex(ValueError, "不能同时存在"):
                generator.load_trusted_nodes(source, {})

    def test_duplicate_anchor_is_rejected(self) -> None:
        name = generator.node_name("us", "vless", 0, "Exit")
        with self.assertRaisesRegex(ValueError, "anchor 重复"):
            generator.ensure_unique_anchors([{"name": name}, {"name": name}])

    def test_fallback_anchor_does_not_collide_with_real_target_region(self) -> None:
        source = {
            "name": generator.node_name("us", "vless", 0, "Exit"),
            "type": "vless",
            "server": "source.example",
            "port": 443,
            "uuid": "source-uuid",
        }
        real_target = {
            "name": generator.node_name("uk", "vless", 0, "Exit"),
            "type": "vless",
            "server": "target.example",
            "port": 443,
            "uuid": "target-uuid",
        }
        fallback = generator.fallback_node(source, "UK")

        self.assertNotEqual(
            generator.anchor_name(real_target["name"]),
            generator.anchor_name(fallback["name"]),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "fallback.yaml"
            generator.write_template([real_target, fallback], [], output)
            self.assertEqual(len(yaml.safe_load(output.read_text())["proxies"]), 2)

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

            inventory.write_text("proxies: []\n")
            self.assertEqual(generator.client_inventory_nodes(host_dir, env, {}), [])

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
                "client-fingerprint": "firefox",
                "reality-opts": {"public-key": "public-key", "short-id": "01"},
            }
        )

        self.assertIn("flow=xtls-rprx-vision", body)
        self.assertIn("public-key=\"public-key\"", body)
        self.assertNotIn("client-fingerprint", body)
        self.assertNotIn("block-quic", body)

    def test_output_path_collision_is_rejected(self) -> None:
        with self.assertRaisesRegex(SystemExit, "输出路径冲突"):
            generator.validate_output_paths(
                [("主输出", Path("output.yaml")), ("raw-output", Path("./output.yaml"))]
            )

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


if __name__ == "__main__":
    unittest.main()
