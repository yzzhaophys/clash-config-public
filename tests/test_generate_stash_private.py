import copy
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import generate_stash_private
from node_io import load_yaml


ROOT = Path(__file__).resolve().parents[1]
UK_EXIT = "🇬🇧🔰.DirectExit-[UK]"
UK_CHAIN = "🇬🇧🔗.Chain-[UK]"
UK_LINE = "🇬🇧.Line-[UK]"
SG_HOME = "🇸🇬🔰.DirectExit-[SG.HomeIP]"


class PrivateStashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shell = load_yaml(ROOT / "home-stash.yaml")
        cls.home = load_yaml(ROOT / "home.yaml")

    def test_empty_static_groups_and_outer_line_reject(self):
        config, count = generate_stash_private.build_private_config(
            self.shell, {"proxies": []}, self.home
        )
        groups = {group["name"]: group for group in config["proxy-groups"]}
        for name in (UK_EXIT, UK_CHAIN, UK_LINE):
            self.assertEqual(groups[name]["type"], "select")
            self.assertEqual(groups[name]["proxies"], ["REJECT"])
            self.assertNotIn("include-all", groups[name])
        self.assertGreaterEqual(count, 3)
        self.assertEqual(
            groups["📡.<DNS>--ChinaDNS"]["proxies"][:2], ["REJECT", "DIRECT"]
        )
        self.assertEqual(self.shell["proxies"], [])

    def test_populated_static_group_uses_explicit_members_and_prunes_empty_child(self):
        nodes = {"proxies": [{
            "name": "VPS-[UK.Core]-SOCKS5-00-test", "type": "socks5",
            "server": "127.0.0.1", "port": 1080,
        }]}
        config, _ = generate_stash_private.build_private_config(self.shell, nodes, self.home)
        groups = {group["name"]: group for group in config["proxy-groups"]}
        self.assertEqual(groups[UK_EXIT]["type"], "url-test")
        self.assertNotIn("include-all", groups[UK_EXIT])
        self.assertNotIn("filter", groups[UK_EXIT])
        self.assertEqual(groups[UK_EXIT]["proxies"], [nodes["proxies"][0]["name"]])
        self.assertEqual(groups[UK_CHAIN]["proxies"], ["REJECT"])
        self.assertEqual(groups[UK_LINE]["type"], "fallback")
        self.assertEqual(groups[UK_LINE]["proxies"], [UK_EXIT])

    def test_static_members_follow_source_filter_and_input_order(self):
        names = [
            "VPS-[UK.Core]-first",
            "VPS-[UK.Core]-Direct=false",
            "VPS-[UK.Exit]-second",
            "VPS-[UK.Core]-PrxChain",
            "VPS-[SG.HomeIP]-first",
            "VPS-[SG.HomeIP]-Direct=false",
            "VPS-[SG.HomeIP]-pRxChAiN",
        ]
        nodes = {"proxies": [
            {"name": name, "type": "socks5", "server": "127.0.0.1", "port": 1080}
            for name in names
        ]}
        config, _ = generate_stash_private.build_private_config(self.shell, nodes, self.home)
        groups = {group["name"]: group for group in config["proxy-groups"]}
        self.assertEqual(groups[UK_EXIT]["proxies"], [names[0], names[2]])
        self.assertEqual(groups[SG_HOME]["proxies"], [names[4]])
        self.assertFalse(any("filter" in group or "include-all" in group
                             for group in config["proxy-groups"]))

    def test_stash_protocol_fields_are_mapped_without_changing_input(self):
        nodes = {"proxies": [
            {"name": "VPS-[UK.Core]-VLESS-00", "type": "vless",
             "server": "127.0.0.1", "port": 443, "uuid": "example-id",
             "tls": True, "servername": " example.test "},
            {"name": "VPS-[SG.HomeIP]-H2-00", "type": "hysteria2",
             "server": "127.0.0.1", "port": 443, "password": " secret ",
             "sni": "example.test"},
        ]}
        original = copy.deepcopy(nodes)
        config, _ = generate_stash_private.build_private_config(self.shell, nodes, self.home)
        vless, hysteria = config["proxies"]
        self.assertEqual(vless["sni"], " example.test ")
        self.assertNotIn("servername", vless)
        self.assertEqual(hysteria["auth"], " secret ")
        self.assertNotIn("password", hysteria)
        self.assertEqual(nodes, original)

    def test_invalid_protocol_mapping_and_dialer_cycle_are_rejected(self):
        proxy = {"name": "VPS-[UK.Core]-H2-00", "type": "hysteria2",
                 "server": "127.0.0.1", "port": 443, "password": "secret"}
        for changed in ({"auth": "different"}, {"password": None}, {"password": 42}):
            with self.subTest(changed=changed):
                candidate = {**proxy, **changed}
                with self.assertRaises(ValueError):
                    generate_stash_private.build_private_config(
                        self.shell, {"proxies": [candidate]}, self.home
                    )
        vless = {"name": "VPS-[UK.Core]-VLESS-00", "type": "vless",
                 "server": "127.0.0.1", "port": 443, "uuid": "example-id",
                 "servername": "one.test", "sni": "two.test"}
        with self.assertRaises(ValueError):
            generate_stash_private.build_private_config(
                self.shell, {"proxies": [vless]}, self.home
            )
        loop = {**proxy, "dialer-proxy": UK_LINE}
        with self.assertRaisesRegex(ValueError, "循环引用"):
            generate_stash_private.build_private_config(
                self.shell, {"proxies": [loop]}, self.home
            )

    def test_provider_and_changed_filter_are_rejected(self):
        shell = copy.deepcopy(self.shell)
        shell["proxy-providers"] = {"dynamic": {"type": "http"}}
        with self.assertRaises(ValueError):
            generate_stash_private.build_private_config(shell, {"proxies": []}, self.home)
        shell = copy.deepcopy(self.shell)
        shell["proxy-groups"].append({
            "name": "unreviewed-auto", "type": "url-test",
            "include-all": True, "filter": "never-matches",
        })
        with self.assertRaises(ValueError):
            generate_stash_private.build_private_config(shell, {"proxies": []}, self.home)
        shell = copy.deepcopy(self.shell)
        group = next(group for group in shell["proxy-groups"] if group["name"] == UK_EXIT)
        group["filter"] = ".*"
        with self.assertRaises(ValueError):
            generate_stash_private.build_private_config(shell, {"proxies": []}, self.home)

    def test_stale_public_shell_is_rejected(self):
        shell = copy.deepcopy(self.shell)
        shell["mode"] = "direct" if shell.get("mode") != "direct" else "rule"
        with self.assertRaisesRegex(ValueError, "骨架与 home.yaml 不一致"):
            generate_stash_private.build_private_config(shell, {"proxies": []}, self.home)
        home = copy.deepcopy(self.home)
        home["dns"]["enable"] = not home["dns"]["enable"]
        with self.assertRaisesRegex(ValueError, "骨架与 home.yaml 不一致"):
            generate_stash_private.build_private_config(self.shell, {"proxies": []}, home)

    def test_writer_is_private_and_old_file_survives_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = root / "nodes.yaml"
            nodes.write_text("proxies: []\n", encoding="utf-8")
            output = root / "stash.yaml"
            output.write_text("sentinel\n", encoding="utf-8")
            with patch.object(generate_stash_private.os, "replace", side_effect=OSError("injected")):
                with self.assertRaises(OSError):
                    generate_stash_private.write_private_config(
                        ROOT / "home-stash.yaml", nodes, ROOT / "home.yaml", output
                    )
            self.assertEqual(output.read_text(encoding="utf-8"), "sentinel\n")
            self.assertEqual(set(root.iterdir()), {nodes, output})
            generate_stash_private.write_private_config(
                ROOT / "home-stash.yaml", nodes, ROOT / "home.yaml", output
            )
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            config = load_yaml(output)
            group = next(g for g in config["proxy-groups"] if g["name"] == UK_LINE)
            self.assertEqual(group["proxies"], ["REJECT"])

    def test_writer_expands_repeated_alpn_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = root / "nodes.yaml"
            nodes.write_text(
                "proxies:\n"
                "- {name: 'VPS-[US.Core]-VLESS-00-test', type: vless, "
                "server: example.invalid, port: 443, uuid: sample, alpn: &alpn [h2, http/1.1]}\n"
                "- {name: 'VPS-[US.Core]-VLESS-01-test', type: vless, "
                "server: example.invalid, port: 443, uuid: sample, alpn: *alpn}\n",
                encoding="utf-8",
            )
            output = root / "stash.yaml"
            generate_stash_private.write_private_config(
                ROOT / "home-stash.yaml", nodes, ROOT / "home.yaml", output,
            )
            rendered = output.read_text(encoding="utf-8")
            self.assertEqual(rendered.count("alpn:\n"), 2)
            self.assertNotIn("alpn: *", rendered)
            config = load_yaml(output)
            self.assertEqual(
                [node["alpn"] for node in config["proxies"]],
                [["h2", "http/1.1"], ["h2", "http/1.1"]],
            )

    def test_duplicate_yaml_and_output_aliases_leave_old_file_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nodes = root / "nodes.yaml"
            nodes.write_text("proxies: []\nproxies: []\n", encoding="utf-8")
            output = root / "stash.yaml"
            output.write_text("sentinel\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                generate_stash_private.write_private_config(
                    ROOT / "home-stash.yaml", nodes, ROOT / "home.yaml", output
                )
            self.assertEqual(output.read_text(encoding="utf-8"), "sentinel\n")
            nodes.write_text("proxies: []\n", encoding="utf-8")
            alias = root / "alias.yaml"
            os.symlink(output, alias)
            with self.assertRaises(ValueError):
                generate_stash_private.write_private_config(
                    ROOT / "home-stash.yaml", nodes, ROOT / "home.yaml", alias
                )
            os.unlink(alias)
            os.link(output, alias)
            with self.assertRaises(ValueError):
                generate_stash_private.write_private_config(
                    ROOT / "home-stash.yaml", nodes, ROOT / "home.yaml", alias
                )
            self.assertEqual(output.read_text(encoding="utf-8"), "sentinel\n")


if __name__ == "__main__":
    unittest.main()
