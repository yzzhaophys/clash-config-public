import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

import generate_raw_nodes as g
import manage_trusted_nodes as m
from node_conversion import xray_options, hysteria_options
from node_io import load_yaml


UUID = '11111111-1111-4111-8111-111111111111'


def trusted(identity='test', protocol='vless'):
    proxy = {'type': protocol, 'server': 'node.example', 'port': 443}
    proxy.update({'uuid': UUID} if protocol == 'vless' else
                 {'username': ' user ', 'password': ' pass "\\测试 '})
    return {'id': identity, 'region': 'US', 'proxy': proxy}


def inbound(stream=None):
    data = {'protocol': 'vless', 'port': 443,
            'settings': {'decryption': 'none', 'clients': [{'id': UUID}]}}
    if stream is not None:
        data['streamSettings'] = stream
    return data


def convert(stream=None):
    data = inbound(stream)
    return xray_options(data, data['settings']['clients'][0], 'fixture')


class FileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, name, text):
        path = self.root / name
        g.secure_write(path, text)
        return path

    def test_duplicate_yaml_rejected_by_both_entrypoints(self):
        document = yaml.safe_dump({'nodes': [trusted()]})
        source = self.write('source.yaml', document + document)
        target = self.write('target.yaml', yaml.safe_dump({'nodes': [trusted('keep')]}))
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError, '重复字段'):
            g.load_trusted_nodes(source, {})
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaisesRegex(m.TrustedNodesError, '重复字段'):
                m.merge_trusted_nodes_file(target, source, apply=apply)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(list(self.root.glob('*.bak-*')))

    def test_nested_duplicates_and_syntax_errors_do_not_echo_passwords(self):
        for text in ('proxy:\n  password: SECRET_VALUE\n  password: OTHER_SECRET\n',
                     'password: [SECRET_VALUE\n'):
            p = self.write('bad.yaml', text)
            with self.assertRaises(ValueError) as caught:
                load_yaml(p)
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertIn('行', str(caught.exception))

    def test_legitimate_merge_override_and_reused_anchors(self):
        p = self.write('merge.yaml', 'base: &base {name: original, port: 443}\n'
                       'one: {<<: *base, name: one}\ntwo: {<<: *base, name: two}\n')
        parsed = load_yaml(p)
        self.assertEqual(parsed['one'], {'name': 'one', 'port': 443})
        self.assertEqual(parsed['two']['name'], 'two')

    def test_duplicate_inside_merged_anchor_is_rejected(self):
        p = self.write('merge.yaml', 'base: &base {port: 443, port: 80}\ncopy: {<<: *base}\n')
        with self.assertRaisesRegex(ValueError, '重复字段'):
            load_yaml(p)

    def test_every_output_protects_input_and_links(self):
        source = self.write('source.yaml', yaml.safe_dump({'nodes': [trusted()]}))
        symlink = self.root / 'symlink.yaml'
        symlink.symlink_to(source)
        hardlink = self.root / 'hardlink.yaml'
        os.link(source, hardlink)
        for label in ('主输出', 'raw-output', 'loon-output'):
            for output in (source, symlink, hardlink):
                with self.subTest(label=label, output=output.name), self.assertRaisesRegex(SystemExit, '输入文件'):
                    g.validate_output_paths([(label, output)], [source])

    def test_hardlinked_outputs_are_rejected(self):
        first = self.write('one', 'keep')
        second = self.root / 'two'
        os.link(first, second)
        with self.assertRaisesRegex(SystemExit, '冲突'):
            g.validate_output_paths([('one', first), ('two', second)])

    def test_cli_cannot_destroy_trusted_input(self):
        source = self.write('source.yaml', yaml.safe_dump({'nodes': [trusted()]}))
        before = source.read_bytes()
        with self.assertRaisesRegex(SystemExit, '输入文件'):
            g.main(['--plain', '--no-loon', '--hosts-dir', str(self.root / 'hosts'),
                    '--trusted-nodes-file', str(source), '--output', str(source)])
        self.assertEqual(source.read_bytes(), before)

    def test_input_discovery_protects_all_source_kinds(self):
        host = self.root / 'vps-us'
        g.secure_write(host / 'host.env', 'VPS_HOST=node.example')
        g.secure_write(host / 'config/xray/one.json', '{}')
        sources = g.input_paths(self.root, self.root / 'airport', self.root / 'trusted.yaml', self.root / 'vars')
        for suffix in ('host.env', 'secrets/client/clash-nodes.yaml', 'secrets/xray-inbounds.json',
                       'secrets/hysteria.yaml', 'config/hysteria/config.yaml', 'config/xray/one.json'):
            self.assertIn(host / suffix, sources)
        self.assertIn(self.root / 'vars/vps-us.yml', sources)
        self.assertIn(self.root / 'airport/selected-nodes.yaml', sources)

    def test_late_render_failure_does_not_replace_any_output(self):
        source = self.write('source.yaml', yaml.safe_dump({'nodes': [trusted()]}))
        output = self.write('out.yaml', 'OLD MAIN')
        raw = self.write('raw.yaml', 'OLD RAW')
        loon = self.write('loon.conf', 'OLD LOON')
        with mock.patch.object(g, 'write_loon', side_effect=ValueError('conversion failed')):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(SystemExit, 'Loon'):
                g.main(['--plain', '--hosts-dir', str(self.root / 'hosts'),
                        '--trusted-nodes-file', str(source), '--output', str(output),
                        '--raw-output', str(raw), '--loon-output', str(loon)])
        self.assertEqual(output.read_text(), 'OLD MAIN')
        self.assertEqual(raw.read_text(), 'OLD RAW')
        self.assertEqual(loon.read_text(), 'OLD LOON')

    def test_clash_nested_parameters_survive_all_yaml_outputs(self):
        source = trusted()
        source['proxy'].update({'udp': False, 'tfo': False, 'network': 'ws', 'tls': True,
                               'ws-opts': {'path': '', 'headers': {'X-Token': ' value '}, 'max-early-data': 0},
                               'smux': {'enabled': False}, 'alpn': [], 'custom-extension': {'nested': [0, False, '']}})
        expected = copy.deepcopy(source['proxy'])
        normalized = g.normalize_trusted_nodes([source], {}, self.root / 'input')[0]
        for writer in ('plain', 'template'):
            path = self.root / f'{writer}.yaml'
            if writer == 'plain':
                g.write_plain([normalized], path)
            else:
                g.write_template([normalized], [], path)
            output = load_yaml(path)['proxies'][0]
            output.pop('name')
            self.assertEqual(output, expected)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ConversionTests(unittest.TestCase):
    def test_early_data_preserves_other_query_encoding(self):
        opts = convert({'network': 'ws', 'wsSettings': {
            'path': '/socket?token=a%20b%2fc&ed=12&flag&empty='
        }})['ws-opts']
        self.assertEqual(opts['path'], '/socket?token=a%20b%2fc&flag&empty=')
        self.assertEqual(opts['max-early-data'], 12)

    def test_invalid_security_type_is_clean_error(self):
        with self.assertRaises(ValueError):
            convert({'security': {}})

    def test_optional_xray_parameters_are_not_invented(self):
        result = convert()
        self.assertEqual(set(result), {'_conversion-audit'})
        result = convert({'security': 'tls', 'tlsSettings': {'allowInsecure': False, 'alpn': []}})
        self.assertFalse(result['skip-cert-verify'])
        self.assertEqual(result['alpn'], [])
        for key in ('servername', 'client-fingerprint', 'flow', 'encryption', 'network', 'udp'):
            self.assertNotIn(key, result)

    def test_xray_websocket_path_host_headers_early_data(self):
        result = convert({'network': 'ws', 'wsSettings': {
            'path': '/custom', 'host': 'edge.example', 'headers': {'X-Test': ' value '},
            'maxEarlyData': 0, 'earlyDataHeaderName': '',
        }})
        self.assertEqual(result['ws-opts'], {
            'path': '/custom', 'headers': {'Host': 'edge.example', 'X-Test': ' value '},
            'max-early-data': 0, 'early-data-header-name': '',
        })

    def test_xray_websocket_ed_query_is_mapped(self):
        opts = convert({'network': 'ws', 'wsSettings': {'path': '/ws?token=abc&ed=2048'}})['ws-opts']
        self.assertEqual(opts['path'], '/ws?token=abc')
        self.assertEqual(opts['max-early-data'], 2048)
        self.assertEqual(opts['early-data-header-name'], 'Sec-WebSocket-Protocol')

    def test_xray_conflicting_ws_headers_rejected(self):
        with self.assertRaisesRegex(ValueError, '冲突'):
            convert({'network': 'ws', 'wsSettings': {'host': 'a', 'headers': {'Host': 'b'}}})

    def test_grpc_and_h2_are_mapped(self):
        result = convert({'network': 'grpc', 'grpcSettings': {'serviceName': 'svc'}})
        self.assertEqual(result['grpc-opts'], {'grpc-service-name': 'svc'})
        result = convert({'network': 'http', 'httpSettings': {'host': ['h.example'], 'path': '/h2'}})
        self.assertEqual(result['network'], 'h2')
        self.assertEqual(result['h2-opts'], {'host': ['h.example'], 'path': '/h2'})

    def test_unmapped_fields_fail_without_echoing_values(self):
        for stream in ({'network': 'xhttp'}, {'network': 'ws', 'wsSettings': {'unknown': 'SECRET'}},
                       {'security': 'tls', 'tlsSettings': {'unknown': 'SECRET'}},
                       {'network': 'grpc', 'grpcSettings': {'multiMode': False}},
                       {'network': 'tcp', 'wsSettings': {'path': '/ws'}}):
            with self.subTest(stream=stream), self.assertRaises(ValueError) as caught:
                convert(stream)
            self.assertNotIn('SECRET', str(caught.exception))

    def test_reality_missing_public_key_is_an_error(self):
        with self.assertRaisesRegex(ValueError, 'publicKey') as caught:
            convert({'security': 'reality', 'realitySettings': {'privateKey': 'SECRET', 'serverNames': ['s.example']}})
        self.assertNotIn('SECRET', str(caught.exception))

    def test_reality_preserves_empty_short_id_and_omits_fingerprint(self):
        result = convert({'security': 'reality', 'realitySettings': {
            'publicKey': 'public', 'privateKey': 'SECRET', 'shortIds': ['', 'aa'],
            'serverNames': ['s.example', 'alternate.example'],
        }})
        self.assertEqual(result['reality-opts'], {'public-key': 'public', 'short-id': ''})
        self.assertEqual(result['servername'], 's.example')
        self.assertNotIn('client-fingerprint', result)
        self.assertNotIn('flow', result)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertTrue(result['_conversion-audit']['server-only'])

    def test_xray_keeps_explicit_connection_address(self):
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory) / 'vps-us'
            g.secure_write(host / 'secrets/xray-inbounds.json', json.dumps({'inbounds': [inbound({
                'network': 'tcp', 'security': 'tls', 'tlsSettings': {'serverName': 'cert.example'},
            })]}))
            result = g.xray_nodes(host, {'VPS_CLASH_REGION': 'us', 'VPS_HOST': '203.0.113.1'}, {})[0]
            self.assertEqual(result['server'], '203.0.113.1')
            self.assertEqual(result['servername'], 'cert.example')

    def test_xray_missing_port_and_uuid_rejected(self):
        for missing in ('port', 'uuid'):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                host = Path(directory) / 'vps-us'
                data = inbound()
                if missing == 'port':
                    del data['port']
                else:
                    del data['settings']['clients'][0]['id']
                g.secure_write(host / 'secrets/xray-inbounds.json', json.dumps({'inbounds': [data]}))
                with self.assertRaises(ValueError):
                    g.xray_nodes(host, {'VPS_CLASH_REGION': 'us', 'VPS_HOST': 'node.example'}, {})

    def test_hysteria_obfs_and_password_preserved(self):
        result = hysteria_options({'auth': {'type': 'password', 'password': ' pw '},
                                   'obfs': {'type': 'salamander', 'salamander': {'password': ' obfs '}}}, 'fixture')
        self.assertEqual(result['password'], ' pw ')
        self.assertEqual(result['obfs'], 'salamander')
        self.assertEqual(result['obfs-password'], ' obfs ')
        self.assertNotIn('sni', result)
        self.assertNotIn('skip-cert-verify', result)

    def test_hysteria_absent_obfs_not_created_and_missing_password_rejected(self):
        result = hysteria_options({'auth': {'password': 'pw'}}, 'fixture')
        self.assertNotIn('obfs', result)
        for data in ({'auth': {'password': 'pw'}, 'obfs': {'type': 'salamander'}},
                     {'auth': {'type': 'http'}}, {'auth': {'password': 'pw'}, 'extra': False}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                hysteria_options(data, 'fixture')

    def test_hysteria_address_separate_from_certificate_sni(self):
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory) / 'vps-us'
            g.secure_write(host / 'secrets/hysteria.yaml', yaml.safe_dump({
                'listen': ':443', 'tls': {'cert': '/live/cert.example/fullchain.pem', 'key': 'SECRET'},
                'auth': {'password': ' pw '},
            }))
            result = g.hy2_node(host, {'VPS_CLASH_REGION': 'us', 'VPS_HOST': '203.0.113.1'}, {})
            self.assertEqual(result['server'], '203.0.113.1')
            self.assertEqual(result['sni'], 'cert.example')
            self.assertNotIn('SECRET', json.dumps(result))

    def test_socks5_credentials_are_byte_for_byte_preserved(self):
        for password in (' pw ', '   ', '"\\测试'):
            source = trusted(protocol='socks5')
            source['proxy']['password'] = password
            result = g.normalize_trusted_nodes([source], {}, Path('fixture'))[0]
            self.assertEqual(result['password'], password)
            self.assertEqual(result['username'], ' user ')

    def test_invalid_credentials_are_not_coerced(self):
        for value in (None, '', False, 123, [], 'bad\n'):
            source = trusted(protocol='socks5')
            source['proxy']['password'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                g.normalize_trusted_nodes([source], {}, Path('fixture'))


class FallbackAndLoonTests(unittest.TestCase):
    def test_loon_invalid_values_are_reported_as_skips(self):
        base = {'name': 'test', 'type': 'vless', 'server': 'node.example',
                'port': 443, 'uuid': UUID}
        for extra in ({'network': {}}, {'encryption': []}, {'tls': None},
                      {'sni': None}, {'sni': 'bad\nvalue'},
                      {'alpn': [{'invalid': True}]}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                count, skipped = g.write_loon([base | extra], Path(tmp) / 'loon.conf')
                self.assertEqual(count, 0)
                self.assertEqual(len(skipped), 1)

    def test_loon_plugin_empty_strings_preserved(self):
        body = g.loon_node_body({'type': 'ss', 'server': 'node.example', 'port': 443,
                                'cipher': 'aes-128-gcm', 'password': ' pass ',
                                'plugin': 'obfs', 'plugin-opts': {'mode': 'http', 'path': ''}})
        self.assertIn('obfs-uri=', body)

    def source(self, **flags):
        return g.normalize_trusted_nodes([trusted() | flags], {}, Path('fixture'))[0]

    def test_disabled_direct_cannot_be_fallback_source(self):
        source = self.source(**{'allow-direct-exit': False})
        self.assertFalse(g.eligible_fallback_source(source))
        with self.assertRaisesRegex(ValueError, '允许直出'):
            g.fallback_node(source, 'UK')

    def test_fallback_preserves_connection_and_clears_capabilities(self):
        source = self.source(**{'allow-download': True})
        source['ws-opts'] = {'path': '/ws', 'headers': {'Host': 's.example'}}
        result = g.fallback_node(source, 'UK')
        for key in ('type', 'server', 'port', 'uuid', 'ws-opts'):
            self.assertEqual(result[key], source[key])
        for key in ('_allow-download', '_allow-showip', '_allow-relay', '_allow-chain-exit'):
            self.assertFalse(result[key])
        self.assertFalse(g.eligible_fallback_source(result))
        self.assertEqual(g.chain_candidates([source, result]), [])

    def test_fallback_matches_actual_home_filters(self):
        import re
        home = load_yaml(g.SCRIPT_DIR / 'home.yaml')
        for region in g.FALLBACK_REGIONS:
            proxy = g.fallback_node(self.source(), region)
            group = next(group for group in home['proxy-groups'] if f'.DirectExit-[{region}]' in group['name'])
            self.assertRegex(proxy['name'], group['filter'])
            self.assertIsNone(re.search(group['exclude-filter'], proxy['name']))

    def test_homeip_does_not_fill_ordinary_region(self):
        homeip = copy.deepcopy(self.source())
        homeip['name'] = g.node_name('sg', 'vless', 0, 'HomeIP')
        homeip['_exit-type'] = 'homeip'
        with mock.patch('builtins.input', side_effect=['2', '1']), contextlib.redirect_stdout(io.StringIO()):
            result = g.interactive_fallback_nodes([homeip, self.source()])
        self.assertTrue(any(g.node_meta(p['name'])['region'] == 'SG' for p in result))
        self.assertTrue(all(p['server'] == self.source()['server'] for p in result))

    def test_no_eligible_fallback_source_does_not_prompt_success(self):
        with mock.patch('builtins.input') as prompt, contextlib.redirect_stdout(io.StringIO()):
            result = g.interactive_fallback_nodes([self.source(**{'allow-direct-exit': False})])
        self.assertEqual(result, [])
        prompt.assert_not_called()

    def test_loon_does_not_silently_drop_top_level_or_nested_fields(self):
        base = {'type': 'vless', 'server': 'node.example', 'port': 443, 'uuid': UUID}
        cases = [{'client-fingerprint': 'chrome'}, {'smux': {'enabled': False}},
                 {'encryption': 'unsupported'}, {'network': 'ws', 'ws-opts': {'max-early-data': 0}},
                 {'network': 'ws', 'ws-opts': {'headers': {'X-Test': 'v'}}},
                 {'reality-opts': {'public-key': 'key', 'extra': 'value'}}]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                g.loon_node_body(base | extra)

    def test_loon_preserves_false_values_and_no_invented_flow(self):
        body = g.loon_node_body({'type': 'vless', 'server': 'node.example', 'port': 443,
                                 'uuid': UUID, 'tls': True, 'udp': False, 'skip-cert-verify': False,
                                 'reality-opts': {'public-key': 'key', 'short-id': ''}})
        self.assertIn('udp=false', body)
        self.assertIn('skip-cert-verify=false', body)
        self.assertNotIn('flow=', body)

    def test_loon_preserves_password_quoting(self):
        password = ' pw "\\测试 '
        body = g.loon_node_body({'type': 'hysteria2', 'server': 'node.example', 'port': 443,
                                 'password': password, 'obfs': 'salamander', 'obfs-password': password})
        self.assertIn(g.loon_quote(password), body)
        self.assertIn('salamander-password=' + g.loon_quote(password), body)


if __name__ == '__main__':
    unittest.main()
