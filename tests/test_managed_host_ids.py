"""Canonical managed IDs preserve protocol data and special input boundaries."""
import json
from pathlib import Path
import tempfile
import unittest

import generate_raw_nodes as generator


class ManagedHostIDsTests(unittest.TestCase):
    def test_legacy_and_renamed_source_generate_identical_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "hosts" / "vps-test"
            client = source / "secrets" / "client"
            client.mkdir(parents=True)
            (source / "host.env").write_text("VPS_CLASH_REGION=us\n")
            (client / "clash-nodes.yaml").write_text(json.dumps({"proxies": [
                {"type": "socks5", "server": "test.invalid", "port": 1080,
                 "username": "fixture", "password": " test value ", "udp": False}
            ]}))
            public = root / "public"
            public.mkdir()
            (public / "test.yml").write_text("vps_clash_region: jp\nvps_clash_allow_relay: false\n")
            old = generator.collect_proxies(source.parent, public)
            self.assertIn("JP", old[0]["name"])
            self.assertEqual(old[0]["_physical-node-id"], "test")
            self.assertEqual(old[0]["password"], " test value ")
            self.assertIs(old[0]["udp"], False)
            source.rename(source.parent / "test")
            new = generator.collect_proxies(source.parent, public)
            self.assertEqual(old, new)
            inputs = generator.input_paths(source.parent, root / "trusted.yaml", public)
            self.assertIn(public / "test.yml", inputs)
            self.assertEqual(generator.proxy_source_directory(new[0]), "test")

    def test_duplicate_active_ids_and_public_declarations_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("vps-test", "test"):
                path = root / name
                path.mkdir()
                (path / "host.env").write_text("VPS_CLASH_REGION=jp\n")
            with self.assertRaises(ValueError):
                generator.managed_host_directories(root)
            (root / "test.yml").write_text("{}")
            (root / "vps-test.yml").write_text("{}")
            with self.assertRaises(ValueError):
                generator.managed_host_vars_path(root, "test")

    def test_special_sources_stay_outside_managed_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("debian-landing-jp-01", "alpine-relay-us-01", "vps-template"):
                path = root / name
                path.mkdir()
                (path / "host.env").write_text("VPS_CLASH_ORDER=invalid\n")
            self.assertEqual(generator.collect_proxies(root), [])
