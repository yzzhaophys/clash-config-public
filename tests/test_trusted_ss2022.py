import base64
import copy
from pathlib import Path
import tempfile
import unittest

import generate_raw_nodes as generator
import manage_trusted_nodes as manager
from test_manage_trusted_nodes import node, write_nodes


class TrustedSS2022Tests(unittest.TestCase):
    def test_complete_trusted_ss2022_merge_generation_and_removal(self):
        for cipher, size in [('2022-blake3-aes-128-gcm', 16), ('2022-blake3-aes-256-gcm', 32)]:
            with self.subTest(cipher=cipher), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                candidate = node('fixture', protocol='ss')
                candidate['proxy'].update(cipher=cipher, password=base64.b64encode(bytes(range(size))).decode(), udp=True)
                source, target = root / 'candidate.yaml', root / 'trusted.yaml'
                write_nodes(source, [candidate]); write_nodes(target, [node('unrelated')])
                self.assertEqual(manager.main(['merge', '--target', str(target), '--source', str(source), '--apply']), 0)
                normalized = generator.normalize_trusted_nodes([candidate], {}, source)
                self.assertEqual(normalized[0]['type'], 'ss')
                self.assertTrue(normalized[0]['udp'])
                self.assertIn(cipher, generator.loon_node_body(normalized[0]))
                self.assertEqual(manager.main(['remove', '--target', str(target), '--id', 'fixture', '--protocol', 'ss', '--apply']), 0)

    def test_invalid_ss2022_keys_rejected_without_rewriting_target(self):
        candidate = node('fixture', protocol='ss')
        candidate['proxy'].update(cipher='2022-blake3-aes-128-gcm', password=base64.b64encode(b'x' * 16).decode(), udp=True)
        for secret in ('wrong-key', base64.b64encode(b'x' * 32).decode()):
            invalid = copy.deepcopy(candidate); invalid['proxy']['password'] = secret
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source, target = root / 'candidate.yaml', root / 'trusted.yaml'
                write_nodes(source, [invalid]); write_nodes(target, [node('unrelated')])
                before = target.read_bytes()
                self.assertNotEqual(manager.main(['merge', '--target', str(target), '--source', str(source), '--apply']), 0)
                self.assertEqual(target.read_bytes(), before)
