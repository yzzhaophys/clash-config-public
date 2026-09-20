"""Exercise a chain-only JP HomeIP through merge, render and real group filters."""

import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import generate_raw_nodes as g
import manage_trusted_nodes as m
from node_io import load_yaml


def fixture(identity, region, *, landing=False):
    return {
        'id': identity, 'region': region,
        'exit-type': 'homeip' if landing else 'general',
        'allow-relay': not landing, 'allow-chain-exit': landing,
        'allow-direct-exit': not landing,
        'allow-download': False, 'allow-showip': False,
        'relay-protocol': 'vless', 'chain-exit-protocol': 'vless',
        'proxy': {'type': 'vless', 'server': 'example.invalid', 'port': 443,
                  'uuid': '11111111-1111-4111-8111-111111111111',
                  'tls': True, 'network': 'tcp', 'udp': True},
    }


class TrustedLaunchTests(unittest.TestCase):
    def test_merge_render_policy_idempotence_and_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, source = root / 'trusted.yaml', root / 'jp.yaml'
            existing = {'nodes': [fixture('relay', 'HK')]}
            jp = fixture('jp-home', 'JP', landing=True)
            g.secure_write(target, yaml.safe_dump(existing))
            g.secure_write(source, yaml.safe_dump({'nodes': [jp]}))
            original = target.read_bytes()
            before = set(root.iterdir())
            preview = m.merge_trusted_nodes_file(target, source)
            self.assertEqual((preview.added, preview.updated, preview.preserved), (1, 0, 1))
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(set(root.iterdir()), before)

            applied = m.merge_trusted_nodes_file(target, source, apply=True)
            self.assertEqual(applied.backup.read_bytes(), original)
            self.assertEqual(applied.backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(load_yaml(target)['nodes'], existing['nodes'] + [jp])
            merged = target.read_bytes()
            before = set(root.iterdir())
            self.assertFalse(m.merge_trusted_nodes_file(target, source, apply=True).changed)
            self.assertEqual(target.read_bytes(), merged)
            self.assertEqual(set(root.iterdir()), before)

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(g.main([
                    '--hosts-dir', str(root / 'hosts'), '--airport-dir', str(root / 'airport'),
                    '--ansible-host-vars-dir', str(root / 'vars'),
                    '--trusted-nodes-file', str(target), '--routes', 'JP.HomeIP<-HK',
                    '--output', str(root / 'template.yaml'), '--raw-output', str(root / 'raw.yaml'),
                    '--loon-output', str(root / 'loon.conf'),
                ]), 0)
            proxies = load_yaml(root / 'template.yaml')['proxies']
            self.assertEqual(len(proxies), 3)
            base = next(p for p in proxies if '[JP.HomeIP]' in p['name'] and 'dialer-proxy' not in p)
            chain = next(p for p in proxies if 'dialer-proxy' in p)
            self.assertIn('[Direct=false]', base['name'])
            self.assertEqual({k: v for k, v in base.items() if k != 'name'}, jp['proxy'])
            self.assertEqual(chain['dialer-proxy'], proxies[0]['name'])
            groups = load_yaml(g.SCRIPT_DIR / 'home.yaml')['proxy-groups']

            def includes(group, name):
                return (group.get('include-all', False)
                        and bool(re.search(group.get('filter', '.*'), name))
                        and not (group.get('exclude-filter')
                                 and re.search(group['exclude-filter'], name)))

            self.assertTrue(any(gp['name'].endswith('.Chain-[JP.HomeIP]')
                                and includes(gp, chain['name']) for gp in groups))
            for group in groups:
                self.assertFalse(includes(group, base['name']), group['name'])
                if any(tag in group['name'] for tag in ('ShowIP', 'Download', 'Relay.VPS')):
                    self.assertFalse(includes(group, chain['name']), group['name'])
            self.assertEqual(len(load_yaml(root / 'raw.yaml')['proxies']), 2)
            self.assertEqual(len((root / 'loon.conf').read_text().splitlines()), 1)
            for filename in ('template.yaml', 'raw.yaml', 'loon.conf'):
                self.assertEqual((root / filename).stat().st_mode & 0o777, 0o600)

            # A later failed update keeps the valid inventory intact.
            jp['allow-showip'] = True
            g.secure_write(source, yaml.safe_dump({'nodes': [jp]}))
            with mock.patch.object(m.os, 'replace', side_effect=OSError('fixture failure')):
                with self.assertRaises(OSError):
                    m.merge_trusted_nodes_file(target, source, apply=True)
            self.assertEqual(target.read_bytes(), merged)
            g.secure_write(target, applied.backup.read_text())
            self.assertEqual(target.read_bytes(), original)

    def test_loon_cannot_export_chain_only_node_even_when_protocol_supported(self):
        node = g.normalize_trusted_nodes([fixture('jp-home', 'JP', landing=True)], {}, Path('fixture'))[0]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'loon.conf'
            count, skipped = g.write_loon([node], output)
            self.assertEqual(count, 0)
            self.assertIn('禁止直出', skipped[0])
            self.assertEqual(output.read_text(), '')


if __name__ == '__main__':
    unittest.main()
