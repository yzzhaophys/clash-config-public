import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import manage_trusted_nodes as manager


def node(node_id: str, *, port: int = 443, protocol: str = "vless") -> dict:
    proxy = {
        "type": protocol,
        "server": f"{node_id}.example",
        "port": port,
    }
    if protocol == "vless":
        proxy["uuid"] = f"uuid-{node_id}"
    elif protocol == "socks5":
        proxy["username"] = f"user-{node_id}"
        proxy["password"] = f"password-{node_id}"
    else:
        proxy["password"] = f"password-{node_id}"
    return {
        "id": node_id,
        "name": node_id,
        "region": "US",
        "exit-type": "general",
        "allow-relay": False,
        "allow-chain-exit": False,
        "allow-direct-exit": True,
        "allow-download": False,
        "allow-showip": False,
        "relay-protocol": protocol,
        "chain-exit-protocol": protocol,
        "proxy": proxy,
    }


def write_nodes(path: Path, nodes: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"nodes": nodes}, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)


class TrustedNodesMergeTests(unittest.TestCase):
    def test_merge_preserves_nested_value_type_changes(self):
        for old, new in ((False, 0), (0, False), (True, 1), (1, 1.0)):
            with self.subTest(old=old, new=new), tempfile.TemporaryDirectory() as directory:
                target, source = Path(directory) / 'target.yaml', Path(directory) / 'source.yaml'
                existing, incoming = node('typed'), node('typed')
                existing['proxy']['extension'] = {'values': [old, '', {}, []]}
                incoming['proxy']['extension'] = {'values': [new, '', {}, []]}
                write_nodes(target, [existing])
                write_nodes(source, [incoming])
                before = target.read_bytes()
                preview = manager.merge_trusted_nodes_file(target, source)
                self.assertEqual(preview.updated, 1)
                self.assertEqual(target.read_bytes(), before)
                result = manager.merge_trusted_nodes_file(target, source, apply=True)
                self.assertEqual((result.updated, result.unchanged), (1, 0))
                self.assertEqual(result.backup.read_bytes(), before)
                stored = yaml.safe_load(target.read_text())['nodes'][0]
                self.assertIs(type(stored['proxy']['extension']['values'][0]), type(new))
                self.assertEqual(stored, incoming)
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
                self.assertFalse(manager.merge_trusted_nodes_file(target, source, apply=True).changed)

    def test_showip_capability_is_accepted_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "trusted-nodes.yaml"
            source = root / "new.yaml"
            showip = node("provider-us-showip")
            showip["allow-showip"] = True
            write_nodes(target, [])
            write_nodes(source, [showip])

            result = manager.merge_trusted_nodes_file(target, source, apply=True)

            self.assertEqual((result.added, result.updated), (1, 0))
            stored = yaml.safe_load(target.read_text())["nodes"][0]
            self.assertTrue(stored["allow-showip"])

    def test_new_node_is_appended_and_existing_node_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "trusted-nodes.yaml"
            source = root / "new.yaml"
            write_nodes(target, [node("provider-us-01")])
            write_nodes(source, [node("provider-jp-01", protocol="hysteria2")])

            result = manager.merge_trusted_nodes_file(target, source, apply=True)

            self.assertEqual((result.added, result.updated, result.unchanged), (1, 0, 0))
            self.assertEqual(
                [item["id"] for item in yaml.safe_load(target.read_text())["nodes"]],
                ["provider-us-01", "provider-jp-01"],
            )
            self.assertIsNotNone(result.backup)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                list(root.glob("trusted-nodes.yaml.bak-*")),
                [result.backup],
            )

    def test_same_id_updates_in_place_without_reordering_other_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "trusted-nodes.yaml"
            source = root / "new.yaml"
            write_nodes(target, [node("one"), node("two")])
            write_nodes(source, [node("one", port=8443)])

            result = manager.merge_trusted_nodes_file(target, source, apply=True)
            values = yaml.safe_load(target.read_text())["nodes"]

            self.assertEqual((result.added, result.updated, result.preserved), (0, 1, 2))
            self.assertEqual([item["id"] for item in values], ["one", "two"])
            self.assertEqual(values[0]["proxy"]["port"], 8443)

    def test_same_physical_id_can_have_one_entry_per_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "trusted-nodes.yaml"
            source = root / "new.yaml"
            write_nodes(target, [node("nat-01", protocol="vless")])
            write_nodes(
                source,
                [
                    node("nat-01", protocol="hysteria2"),
                    node("nat-01", protocol="socks5"),
                ],
            )

            result = manager.merge_trusted_nodes_file(target, source, apply=True)
            values = yaml.safe_load(target.read_text())["nodes"]

            self.assertEqual((result.added, result.updated), (2, 0))
            self.assertEqual(
                [(item["id"], item["proxy"]["type"]) for item in values],
                [
                    ("nat-01", "vless"),
                    ("nat-01", "hysteria2"),
                    ("nat-01", "socks5"),
                ],
            )

    def test_dry_run_does_not_create_or_modify_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "trusted-nodes.yaml"
            source = root / "new.yaml"
            write_nodes(target, [node("one")])
            before = target.read_bytes()
            write_nodes(source, [node("two")])

            result = manager.merge_trusted_nodes_file(target, source)

            self.assertTrue(result.changed)
            self.assertIsNone(result.backup)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(root.glob("trusted-nodes.yaml.bak-*")), [])

    def test_remove_all_protocols_previews_then_applies_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "trusted-nodes.yaml"
            write_nodes(
                target,
                [
                    node("nat-01", protocol="vless"),
                    node("keep-01", protocol="hysteria2"),
                    node("nat-01", protocol="socks5"),
                ],
            )
            before = target.read_bytes()

            preview = manager.remove_trusted_nodes_file(target, "nat-01")
            self.assertEqual((preview.removed, preview.preserved), (2, 1))
            self.assertIsNone(preview.backup)
            self.assertEqual(target.read_bytes(), before)

            result = manager.remove_trusted_nodes_file(target, "nat-01", apply=True)
            values = yaml.safe_load(target.read_text())["nodes"]
            self.assertEqual((result.removed, result.preserved), (2, 1))
            self.assertEqual(
                [(item["id"], item["proxy"]["type"]) for item in values],
                [("keep-01", "hysteria2")],
            )
            self.assertIsNotNone(result.backup)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_remove_can_target_one_protocol_and_rejects_missing_selector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "trusted-nodes.yaml"
            write_nodes(
                target,
                [
                    node("nat-01", protocol="vless"),
                    node("nat-01", protocol="hysteria2"),
                ],
            )

            result = manager.remove_trusted_nodes_file(
                target, "nat-01", protocol="vless", apply=True
            )
            values = yaml.safe_load(target.read_text())["nodes"]
            self.assertEqual(result.removed, 1)
            self.assertEqual(
                [(item["id"], item["proxy"]["type"]) for item in values],
                [("nat-01", "hysteria2")],
            )
            with self.assertRaisesRegex(manager.TrustedNodesError, "未找到"):
                manager.remove_trusted_nodes_file(
                    target, "nat-01", protocol="vless"
                )

    def test_remove_cli_dispatches_without_exposing_node_content(self) -> None:
        result = manager.RemoveResult(
            removed=2,
            preserved=1,
            changed=True,
            backup=None,
        )
        with mock.patch.object(
            manager, "remove_trusted_nodes_file", return_value=result
        ) as remove, mock.patch("builtins.print") as output:
            exit_code = manager.main(
                [
                    "remove",
                    "--target",
                    "/private/trusted-nodes.yaml",
                    "--id",
                    "nat-01",
                    "--protocol",
                    "vless",
                ]
            )

        self.assertEqual(exit_code, 0)
        remove.assert_called_once_with(
            Path("/private/trusted-nodes.yaml"),
            "nat-01",
            protocol="vless",
            apply=False,
        )
        rendered = " ".join(str(call) for call in output.call_args_list)
        self.assertIn("删除=2", rendered)
        self.assertNotIn("nat-01", rendered)

    def test_duplicate_ids_and_proxies_format_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "trusted-nodes.yaml"
            source = root / "new.yaml"
            write_nodes(target, [node("one")])
            write_nodes(source, [node("two"), node("two", port=8443)])
            with self.assertRaisesRegex(manager.TrustedNodesError, "重复"):
                manager.merge_trusted_nodes_file(target, source)

            source.write_text("proxies: []\n", encoding="utf-8")
            with self.assertRaisesRegex(manager.TrustedNodesError, "proxies"):
                manager.merge_trusted_nodes_file(target, source)

    def test_missing_protocol_credentials_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "trusted-nodes.yaml"
            source = root / "new.yaml"
            invalid_nodes = (
                ("vless", "uuid"),
                ("hysteria2", "password"),
            )
            for protocol, message in invalid_nodes:
                with self.subTest(protocol=protocol):
                    value = node("invalid", protocol=protocol)
                    value["proxy"].pop(message)
                    write_nodes(source, [value])
                    with self.assertRaisesRegex(manager.TrustedNodesError, message):
                        manager.merge_trusted_nodes_file(target, source)

    def test_source_and_existing_target_require_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "trusted-nodes.yaml"
            source = root / "new.yaml"
            write_nodes(target, [node("one")])
            write_nodes(source, [node("two")])

            source.chmod(0o644)
            with self.assertRaisesRegex(manager.TrustedNodesError, "chmod 600"):
                manager.merge_trusted_nodes_file(target, source)
            source.chmod(0o600)

            target.chmod(0o640)
            with self.assertRaisesRegex(manager.TrustedNodesError, "chmod 600"):
                manager.merge_trusted_nodes_file(target, source)
            with self.assertRaisesRegex(manager.TrustedNodesError, "chmod 600"):
                manager.remove_trusted_nodes_file(target, "one")

    def test_atomic_write_failure_keeps_original_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "trusted-nodes.yaml"
            source = root / "new.yaml"
            write_nodes(target, [node("one")])
            write_nodes(source, [node("two")])
            before = target.read_bytes()

            with mock.patch.object(manager.os, "replace", side_effect=OSError("boom")):
                with self.assertRaisesRegex(OSError, "boom"):
                    manager.merge_trusted_nodes_file(target, source, apply=True)

            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(root.glob(".trusted-nodes.yaml.*.tmp")), [])

    def test_target_is_private_and_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "trusted-nodes.yaml"
            source = root / "new.yaml"
            write_nodes(source, [node("one")])
            result = manager.merge_trusted_nodes_file(target, source, apply=True)
            self.assertTrue(result.changed)
            self.assertEqual(target.stat().st_mode & stat.S_IMODE(0o777), 0o600)

            symlink = root / "link.yaml"
            symlink.symlink_to(source)
            with self.assertRaisesRegex(manager.TrustedNodesError, "符号链接"):
                manager.merge_trusted_nodes_file(target, symlink)

            lock_path = root / ".trusted-nodes.yaml.lock"
            lock_path.unlink()
            lock_path.symlink_to(source)
            with self.assertRaisesRegex(manager.TrustedNodesError, "锁文件"):
                manager.merge_trusted_nodes_file(target, source, apply=True)


if __name__ == "__main__":
    unittest.main()
