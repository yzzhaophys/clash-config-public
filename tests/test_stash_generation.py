import tempfile
import unittest
import os
from pathlib import Path
from unittest import mock

import generate_raw_nodes as generator
from node_io import load_yaml


class StashGenerationTests(unittest.TestCase):
    def test_default_stash_output_is_private_and_does_not_touch_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'trusted.yaml'
            source.write_text('''nodes:
  - id: test
    region: US
    proxy:
      type: vless
      server: example.com
      port: 443
      uuid: test-secret
''')
            reference = root / 'nodes.stoverride'
            reference.write_text('REFERENCE')
            primary = root / 'nodes.yaml'
            generator.main([
                '--plain', '--no-loon', '--hosts-dir', str(root / 'hosts'),
                '--trusted-nodes-file', str(source), '--output', str(primary),
            ])
            output = root / generator.STASH_OUT.name
            self.assertTrue(output.is_file())
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            self.assertEqual(reference.read_text(), 'REFERENCE')
            data = load_yaml(output)
            self.assertEqual(len(data['proxies']), 1)

    def test_sparse_nodes_prune_empty_groups_and_references(self):
        relay = {
            'name': generator.node_name('jp', 'vless', 0, 'Core'),
            'type': 'vless', 'server': 'relay.example', 'port': 443,
            'uuid': 'relay-secret',
        }
        exit_node = {
            'name': generator.node_name('us', 'vless', 0, 'Exit',
                                        allow_direct_exit=False),
            'type': 'vless', 'server': 'exit.example', 'port': 443,
            'uuid': 'exit-secret', '_allow-direct-exit': False,
        }
        content = generator.render_stash_override(
            [relay, exit_node], [(exit_node, relay)]
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'nodes.stoverride'
            output.write_text(content)
            data = load_yaml(output)

        self.assertTrue(content.startswith('name: stash-nodes.generated\nproxies:\n'))
        self.assertIn('proxy-groups: #!replace', content)
        self.assertNotIn('规则源 Rule Providers', content)
        self.assertNotIn('锚点 Providers', content)
        self.assertTrue(content.rstrip().endswith('}'))
        self.assertEqual(len(data['proxies']), 3)
        self.assertNotIn('_allow-direct-exit', data['proxies'][1])
        groups = {group['name']: group for group in data['proxy-groups']}
        self.assertIn('🇺🇸.Chain-[US]', groups)
        self.assertIn('🇯🇵.DirectExit-[JP]', groups)
        self.assertNotIn('🇺🇸.DirectExit-[US]', groups)
        self.assertNotIn('🇫🇷.DirectExit-[FR]', groups)
        self.assertNotIn('🇫🇷.Line-[FR]', groups)
        self.assertEqual(groups['🇺🇸.Line-[US]']['proxies'], ['🇺🇸.Chain-[US]'])
        self.assertEqual(groups['🌏.Route-[EastAsia]']['proxies'], ['🇯🇵.Line-[JP]'])
        self.assertEqual(groups['🌍.Route-[Europe]']['proxies'],
                         ['♾️.Route-[Final.Fallback]'])
        home_groups = generator.load_mihomo_proxy_groups()
        self.assertEqual(groups['OpenAI']['icon'], home_groups['OpenAI']['icon'])
        for group in groups.values():
            for member in group.get('proxies', []):
                self.assertTrue(member in groups or member in generator.PROXY_GROUP_BUILTINS)

    def test_stash_path_alias_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'trusted.yaml'
            source.write_text('nodes: []\n')
            primary = root / 'nodes.yaml'
            primary.write_text('OLD MAIN')
            with self.assertRaisesRegex(SystemExit, '输出路径冲突'):
                generator.main([
                    '--plain', '--no-loon', '--hosts-dir', str(root / 'hosts'),
                    '--trusted-nodes-file', str(source), '--output', str(primary),
                    '--stash-output', str(primary),
                ])
            self.assertEqual(primary.read_text(), 'OLD MAIN')

    def test_stash_render_failure_keeps_existing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'trusted.yaml'
            source.write_text('''nodes:
  - id: test
    region: US
    proxy:
      type: vless
      server: example.com
      port: 443
      uuid: test-secret
''')
            primary = root / 'nodes.yaml'
            stash = root / 'nodes.stoverride'
            primary.write_text('OLD MAIN')
            stash.write_text('OLD STASH')
            with mock.patch.object(generator, 'render_stash_override',
                                   side_effect=ValueError('bad groups')):
                with self.assertRaisesRegex(SystemExit, 'Stash 输出'):
                    generator.main([
                        '--plain', '--no-loon', '--hosts-dir', str(root / 'hosts'),
                        '--trusted-nodes-file', str(source), '--output', str(primary),
                        '--stash-output', str(stash),
                    ])
            self.assertEqual(primary.read_text(), 'OLD MAIN')
            self.assertEqual(stash.read_text(), 'OLD STASH')


if __name__ == '__main__':
    unittest.main()
