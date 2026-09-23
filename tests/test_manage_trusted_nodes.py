import contextlib
import io
import os
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


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'trusted.yaml'
        write_nodes(self.target, [node('one'), node('one', protocol='socks5'), node('two')])

    def run_menu(self, answers):
        output = io.StringIO()
        with mock.patch('builtins.input', side_effect=answers), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = manager.main(['--target', str(self.target)])
        self.assertEqual(code, 0)
        return output.getvalue()

    def test_default_paths_and_explicit_override(self):
        with mock.patch.dict(os.environ, {'CLASH_TRUSTED_NODES_FILE': str(self.target)}):
            for command in ([], ['list'], ['interactive'], ['merge', '--source', 'new.yaml'],
                            ['remove', '--id', 'one']):
                self.assertEqual(manager.parse_args(command).target, self.target)
            for command in (['--target', 'other.yaml', 'list'], ['list', '--target', 'other.yaml']):
                self.assertEqual(manager.parse_args(command).target, Path('other.yaml'))
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(manager.generator, 'default_hosts_dir', return_value=self.root), \
                mock.patch.object(manager.generator, 'default_airport_dir', return_value=self.root / 'airport'):
            self.assertEqual(manager.parse_args([]).target, self.root / 'airport/trusted-nodes.yaml')

    def test_import_directory_default_environment_and_override(self):
        with mock.patch.dict(os.environ, {'CLASH_TRUSTED_IMPORT_DIR': str(self.root / 'env')}):
            self.assertEqual(manager.parse_args([]).import_dir, self.root / 'env')
            for command in (['--import-dir', str(self.root / 'cli')],
                            ['interactive', '--import-dir', str(self.root / 'cli')]):
                self.assertEqual(manager.parse_args(command).import_dir, self.root / 'cli')
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(manager.parse_args(['--target', str(self.target)]).import_dir,
                             self.root / 'imports')

    def test_numbered_import_and_field_preview_hide_values(self):
        directory = self.root / 'imports'
        directory.mkdir()
        changed = node('one', port=8443)
        changed['proxy']['uuid'] = 'SECRET-new-uuid'
        changed['proxy']['ws-opts'] = {'headers': {'SECRET-key': 'SECRET-value'}}
        source = directory / 'landing-jp-node.yaml'
        write_nodes(source, [changed, node('new')])
        (directory / 'alias.yaml').symlink_to(source)
        os.link(self.target, directory / 'target-alias.yaml')
        output = self.run_menu(['3', '1', 'y', '0'])
        self.assertIn('landing-jp-node.yaml', output)
        self.assertNotIn('alias.yaml', output)
        for field in ('proxy.port', 'proxy.uuid', 'proxy.ws-opts'):
            self.assertIn(field, output)
        self.assertIn("新增：ID='new'", output)
        self.assertIn("更新：ID='one'", output)
        self.assertNotIn('SECRET', output)
        self.assertNotIn('8443', output)
        self.assertEqual(yaml.safe_load(self.target.read_text())['nodes'][0], changed)

    def test_restore_cancel_apply_and_preserve_current_backup(self):
        source = self.root / (self.target.name + '.bak-20260101T000000Z')
        restored = {'nodes': [node('restored')], 'extension': {'nested': [False, 0, '', {}]}}
        source.write_text(yaml.safe_dump(restored))
        source.chmod(0o600)
        before = self.target.read_bytes()
        output = self.run_menu(['4', '1', '', '0'])
        self.assertIn("删除：ID='one'", output)
        self.assertIn("新增：ID='restored'", output)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(len(list(self.root.glob('*.bak-*'))), 1)
        answers = iter(['4', '1', 'y', '0'])
        def answer(prompt):
            if '确认恢复' in prompt:
                write_nodes(source, [node('unreviewed')])
            return next(answers)
        self.run_menu(answer)
        self.assertEqual(yaml.safe_load(self.target.read_text()), restored)
        backup = next(p for p in self.root.glob('*.bak-*') if p != source)
        self.assertEqual(backup.read_bytes(), before)
        for path in (backup, self.target):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_restore_validation_and_replace_failure_keep_current_inventory(self):
        source = self.root / (self.target.name + '.bak-fixture')
        before = self.target.read_bytes()
        for contents, mode in [('nodes: []\nnodes: []\n', 0o600),
                               ('nodes: []\n', 0o644),
                               ('nodes: [{id: invalid}]\n', 0o600)]:
            source.write_text(contents)
            source.chmod(mode)
            self.assertIn('操作失败', self.run_menu(['4', '1', '0']))
            self.assertEqual(self.target.read_bytes(), before)
        write_nodes(source, [node('restored')])
        with mock.patch.object(manager.os, 'replace', side_effect=OSError('injected')):
            self.assertIn('操作失败', self.run_menu(['4', '1', 'y', '0']))
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_restore_without_current_file_and_excludes_aliases(self):
        symlink = self.root / (self.target.name + '.bak-link')
        symlink.symlink_to(self.target)
        hardlink = self.root / (self.target.name + '.bak-hard')
        os.link(self.target, hardlink)
        self.assertIn('没有可用备份', self.run_menu(['4', '0']))
        hardlink.unlink()
        source = self.root / (self.target.name + '.bak-fixture')
        write_nodes(source, [node('restored')])
        self.target.unlink()
        self.run_menu(['4', '1', 'y', '0'])
        self.assertEqual(yaml.safe_load(self.target.read_text())['nodes'], [node('restored')])

    def test_list_displays_metadata_without_credentials_or_addresses(self):
        output = self.run_menu(['1', '0'])
        self.assertIn("1. 'one' | US | vless", output)
        self.assertIn('Direct', output)
        for value in ('uuid-one', 'password-one', 'user-one', 'one.example'):
            self.assertNotIn(value, output)
        self.assertFalse(list(self.root.glob('*.bak-*')))

    def test_remove_one_protocol_or_all_for_id(self):
        for scope, remaining in [('1', [('one', 'socks5'), ('two', 'vless')]),
                                 ('2', [('two', 'vless')])]:
            with self.subTest(scope=scope):
                write_nodes(self.target, [node('one'), node('one', protocol='socks5'), node('two')])
                self.run_menu(['2', '1', scope, 'y', '0'])
                nodes = yaml.safe_load(self.target.read_text())['nodes']
                self.assertEqual([(n['id'], n['proxy']['type']) for n in nodes], remaining)
                self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
                backups = list(self.root.glob('*.bak-*'))
                self.assertTrue(backups)
                self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in backups))

    def test_cancel_invalid_selection_and_eof_do_not_change_inventory(self):
        before = self.target.read_bytes()
        for answers in (['2', '999', '0'], ['2', 'bad', '0'], ['2', '', '0'],
                        ['2', '1', '1', '', '0'], ['2', '1', '2', EOFError()],
                        ['2', '1', '1', KeyboardInterrupt()]):
            with self.subTest(answers=answers):
                self.run_menu(answers)
                self.assertEqual(self.target.read_bytes(), before)
                self.assertFalse(list(self.root.glob('*.bak-*')))

    def test_import_cancel_apply_and_source_snapshot(self):
        source = self.root / 'incoming.yaml'
        incoming = node('new')
        incoming['proxy']['extension'] = {'nested': [False, 0, '', {}, []]}
        write_nodes(source, [incoming])
        before = self.target.read_bytes()
        self.run_menu(['3', str(source), '', '0'])
        self.assertEqual(self.target.read_bytes(), before)
        self.assertFalse(list(self.root.glob('*.bak-*')))
        answers = iter(['3', str(source), 'y', '0'])
        def answer(prompt):
            if '确认应用' in prompt:
                write_nodes(source, [node('unreviewed')])
            return next(answers)
        self.run_menu(answer)
        nodes = yaml.safe_load(self.target.read_text())['nodes']
        self.assertEqual(nodes[-1], incoming)
        self.assertTrue(list(self.root.glob('*.bak-*')))

    def test_import_rejects_aliases_bad_permissions_and_duplicates(self):
        source = self.root / 'incoming.yaml'
        write_nodes(source, [node('new')])
        source.chmod(0o644)
        symlink = self.root / 'link.yaml'
        symlink.symlink_to(self.target)
        hardlink = self.root / 'hard.yaml'
        os.link(self.target, hardlink)
        duplicate = self.root / 'duplicate.yaml'
        duplicate.write_text('nodes: []\nnodes: []\n')
        duplicate.chmod(0o600)
        before = self.target.read_bytes()
        for invalid in (source, symlink, hardlink, self.target, duplicate):
            with self.subTest(invalid=invalid):
                output = self.run_menu(['3', str(invalid), '0'])
                self.assertIn('操作失败', output)
                self.assertEqual(self.target.read_bytes(), before)
                self.assertFalse(list(self.root.glob('*.bak-*')))

    def test_empty_inventory_and_noninteractive_list(self):
        self.target.unlink()
        self.assertIn('当前没有节点', self.run_menu(['1', '2', '0']))
        self.assertFalse(self.target.exists())
        with mock.patch('builtins.input') as prompt, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(manager.main(['list', '--target', str(self.target)]), 0)
        prompt.assert_not_called()

    def test_menu_keeps_lock_during_confirmation_and_releases_on_cancel(self):
        real_lock = manager._exclusive_lock
        active = []
        @contextlib.contextmanager
        def tracked(target):
            with real_lock(target):
                active.append(True)
                try:
                    yield
                finally:
                    active.pop()
        answers = iter(['2', '1', '1', 'n', '0'])
        def answer(prompt):
            if '确认删除' in prompt:
                self.assertEqual(active, [True])
            return next(answers)
        with mock.patch.object(manager, '_exclusive_lock', tracked):
            self.run_menu(answer)
        self.assertEqual(active, [])


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
