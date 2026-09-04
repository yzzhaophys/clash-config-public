import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
