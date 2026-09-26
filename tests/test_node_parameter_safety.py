import contextlib
import copy
import io
import json
import math
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
    def test_invalid_later_destination_keeps_all_old_outputs(self):
        source = self.write('input.yaml', yaml.safe_dump({'nodes': [trusted()]}))
        main = self.write('main.yaml', 'OLD MAIN')
        raw = self.write('raw.yaml', 'OLD RAW')
        directory = self.root / 'directory'
        directory.mkdir()
        alias = self.root / 'directory-link'
        alias.symlink_to(directory, target_is_directory=True)
        for invalid in (directory, alias, raw / 'child.yaml'):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(SystemExit, '输出路径'):
                g.main(['--plain', '--hosts-dir', str(self.root / 'hosts'),
                        '--trusted-nodes-file', str(source), '--output', str(main),
                        '--raw-output', str(raw), '--loon-output', str(invalid)])
            self.assertEqual(main.read_text(), 'OLD MAIN')
            self.assertEqual(raw.read_text(), 'OLD RAW')

    def test_outputs_cannot_be_ancestors_of_each_other(self):
        parent = self.root / 'new-output'
        child = parent / 'child.yaml'
        for paths in ((parent, child), (child, parent)):
            with self.subTest(paths=paths), self.assertRaisesRegex(SystemExit, '输出路径冲突'):
                g.validate_output_paths(list(zip(('main', 'raw'), paths)))
        self.assertFalse(parent.exists())

    def test_nested_floats_keep_values_and_types_in_both_formats_and_chains(self):
        source = trusted()
        values = [1e-7, 1e20, -1e-9, 0.0, -0.0, float('inf'), float('-inf'), float('nan')]
        source['proxy']['custom-extension'] = {'values': values}
        proxy = g.normalize_trusted_nodes([source], {}, self.root / 'input')[0]
        dialer = g.normalize_trusted_nodes([trusted('relay')], {('us', 'vless'): 1}, self.root / 'input')[0]
        for mode in ('plain', 'template'):
            path = self.root / f'{mode}.yaml'
            if mode == 'plain':
                g.write_plain([proxy], path)
            else:
                g.write_template([proxy, dialer], [(proxy, dialer)], path)
            nodes = load_yaml(path)['proxies']
            for node in (nodes[0], nodes[-1]) if mode == 'template' else (nodes[0],):
                actual = node['custom-extension']['values']
                for expected, value in zip(values, actual):
                    self.assertIs(type(value), float)
                    if math.isnan(expected):
                        self.assertTrue(math.isnan(value))
                    else:
                        self.assertEqual(value, expected)
                        self.assertEqual(math.copysign(1, value), math.copysign(1, expected))

    def test_missing_environment_inventory_fails_before_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {'CLASH_TRUSTED_NODES_FILE': str(Path(directory) / 'missing')}), \
                    mock.patch.object(g, 'collect_proxies') as collect, \
                    self.assertRaisesRegex(SystemExit, 'trusted-nodes'):
                g.main(['--plain'])
            collect.assert_not_called()

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

    def test_merge_rejects_hardlinked_source_before_any_write(self):
        target = self.write('target.yaml', yaml.safe_dump({'nodes': [trusted()]}))
        source = self.root / 'source.yaml'
        os.link(target, source)
        before = target.read_bytes()
        for apply in (False, True):
            with self.subTest(apply=apply), self.assertRaisesRegex(m.TrustedNodesError, '同一个文件'):
                m.merge_trusted_nodes_file(target, source, apply=apply)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(set(self.root.iterdir()), {source, target})

    def test_cli_cannot_destroy_trusted_input(self):
        source = self.write('source.yaml', yaml.safe_dump({'nodes': [trusted()]}))
        before = source.read_bytes()
        with self.assertRaisesRegex(SystemExit, '输入文件'):
            g.main(['--plain', '--no-loon', '--hosts-dir', str(self.root / 'hosts'),
                    '--trusted-nodes-file', str(source), '--output', str(source)])
        self.assertEqual(source.read_bytes(), before)

    def test_explicit_missing_trusted_file_fails_before_collecting_or_writing(self):
        output = self.write('out.yaml', 'old output\n')
        with mock.patch.object(g, 'collect_proxies') as collect:
            with self.assertRaisesRegex(SystemExit, '显式指定'):
                g.main(['--plain', '--no-loon', '--hosts-dir', str(self.root / 'hosts'),
                        '--trusted-nodes-file', str(self.root / 'missing.yaml'),
                        '--output', str(output)])
            collect.assert_not_called()
        self.assertEqual(output.read_text(), 'old output\n')

    def test_input_discovery_protects_all_source_kinds(self):
        host = self.root / 'vps-us'
        g.secure_write(host / 'host.env', 'VPS_HOST=node.example')
        g.secure_write(host / 'config/xray/one.json', '{}')
        sources = g.input_paths(self.root, self.root / 'trusted.yaml', self.root / 'vars')
        for suffix in ('host.env', 'secrets/client/clash-nodes.yaml', 'secrets/xray-inbounds.json',
                       'secrets/hysteria.yaml', 'config/hysteria/config.yaml', 'config/xray/one.json'):
            self.assertIn(host / suffix, sources)
        self.assertIn(self.root / 'vars/vps-us.yml', sources)
        self.assertIn(self.root / 'trusted.yaml', sources)
        self.assertFalse(any(path.name in {'subscription.yaml', 'selected-nodes.yaml'} for path in sources))

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


class ClientCredentialTests(unittest.TestCase):
    def test_required_fields_reject_missing_null_empty_and_invalid_types(self):
        missing = object()
        for protocol, fields in [('ss', ('password', 'cipher')), ('trojan', ('password',)),
                                 ('vmess', ('uuid',))]:
            for field in fields:
                for value in (missing, None, '', False, 0, [], {}, 'line\nbreak'):
                    node = {'name': 'demo JP', 'type': protocol, 'server': 'example.invalid',
                            'port': 443, **{key: 'fixture' for key in fields}}
                    if value is missing:
                        del node[field]
                    else:
                        node[field] = value
                    with self.subTest(protocol=protocol, field=field, value=value):
                        with self.assertRaises(ValueError):
                            g.require_proxy_credentials(copy.deepcopy(node), protocol, 'fixture')

    def test_credentials_and_nested_extensions_survive_import(self):
        for protocol, field in [('ss', 'password'), ('trojan', 'password'), ('vmess', 'uuid')]:
            node = {'name': 'demo JP', 'type': protocol, 'server': 'example.invalid',
                    'port': 443, field: ' credential with spaces ',
                    'extension': {'nested': [False, 0, '', [], {}]}}
            if protocol == 'ss':
                node['cipher'] = 'aes-128-gcm'
            with self.subTest(protocol=protocol):
                g.require_proxy_credentials(node, protocol, "fixture")
                result = g.clean_proxy(node)
                self.assertEqual(result[field], node[field])
                self.assertEqual(result['extension'], node['extension'])


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

    def test_mapped_optional_types_reject_null_and_containers(self):
        cases = [
            ('tls', 'tlsSettings', 'serverName'), ('tls', 'tlsSettings', 'fingerprint'),
            ('tls', 'tlsSettings', 'alpn'), ('tls', 'tlsSettings', 'allowInsecure'),
            ('grpc', 'grpcSettings', 'serviceName'), ('http', 'httpSettings', 'host'),
            ('http', 'httpSettings', 'path'), ('ws', 'wsSettings', 'host'),
            ('ws', 'wsSettings', 'earlyDataHeaderName'), ('ws', 'wsSettings', 'maxEarlyData'),
        ]
        for transport, section, key in cases:
            for value in (None, {'secret': 'DO_NOT_ECHO'}, 1.5):
                stream = {'security' if transport == 'tls' else 'network': transport,
                          section: {key: value}}
                with self.subTest(section=section, key=key, value=value), self.assertRaises(ValueError) as caught:
                    convert(stream)
                self.assertNotIn('DO_NOT_ECHO', str(caught.exception))
        for value in (True, -1, '0'):
            with self.subTest(early=value), self.assertRaises(ValueError):
                convert({'network': 'ws', 'wsSettings': {'maxEarlyData': value}})
        for value in (None, [], 123, 'bad\nvalue'):
            with self.subTest(header=value), self.assertRaises(ValueError):
                convert({'network': 'ws', 'wsSettings': {'host': 'host.example', 'headers': {'Host': value}}})
            with self.subTest(sni=value), self.assertRaises(ValueError):
                hysteria_options({'auth': {'password': 'pw'}, 'tls': {'sni': value}}, 'fixture')
        for key in ('flow', 'encryption'):
            data = inbound()
            client = data['settings']['clients'][0] | {key: None}
            with self.subTest(client=key), self.assertRaises(ValueError):
                xray_options(data, client, 'fixture')
        for key in ('shortIds', 'serverNames'):
            with self.subTest(reality=key), self.assertRaises(ValueError):
                convert({'security': 'reality', 'realitySettings': {'publicKey': 'key', key: ['bad\nvalue']}})

    def test_mapped_empty_and_false_values_are_preserved(self):
        result = convert({'security': 'tls', 'tlsSettings': {
            'serverName': '', 'alpn': [], 'allowInsecure': False, 'fingerprint': ''}})
        for key, value in {'servername': '', 'alpn': [], 'skip-cert-verify': False,
                           'client-fingerprint': ''}.items():
            self.assertEqual(result[key], value)
        result = convert({'network': 'ws', 'wsSettings': {
            'headers': {}, 'host': '', 'path': '', 'maxEarlyData': 0, 'earlyDataHeaderName': ''}})
        self.assertEqual(result['ws-opts'], {'headers': {'Host': ''}, 'path': '',
                         'max-early-data': 0, 'early-data-header-name': ''})
        self.assertEqual(convert({'network': 'http', 'httpSettings': {'host': [], 'path': ''}})['h2-opts'],
                         {'host': [], 'path': ''})
        self.assertEqual(convert({'network': 'grpc', 'grpcSettings': {'serviceName': ''}})['grpc-opts'],
                         {'grpc-service-name': ''})

    def test_vless_credentials_preserved_in_inventory_server_and_loon(self):
        credential = ' ' + UUID + ' '
        source = trusted()
        source['proxy']['uuid'] = credential
        result = g.normalize_trusted_nodes([source], {}, Path('fixture'))[0]
        self.assertEqual(result['uuid'], credential)
        self.assertIn(g.loon_quote(credential), g.loon_node_body(result))
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory) / 'vps-us'
            data = inbound()
            data['settings']['clients'][0]['id'] = credential
            g.secure_write(host / 'secrets/xray-inbounds.json', json.dumps({'inbounds': [data]}))
            result = g.xray_nodes(host, {'VPS_CLASH_REGION': 'us', 'VPS_HOST': 'node.example'}, {})[0]
            self.assertEqual(result['uuid'], credential)

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

    def test_xray_invalid_clients_abort_before_replacing_outputs(self):
        for value in ('missing', None, [], False, 0, '', {}, 'SYNTHETIC_SECRET', [None]):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                host = root / 'hosts/vps-us'
                g.secure_write(host / 'host.env', 'VPS_CLASH_REGION=us\nVPS_HOST=node.example\n')
                invalid = inbound()
                if value == 'missing':
                    del invalid['settings']['clients']
                else:
                    invalid['settings']['clients'] = value
                g.secure_write(host / 'secrets/xray-inbounds.json',
                               json.dumps({'inbounds': [inbound(), invalid]}))
                outputs = [root / name for name in ('main.yaml', 'raw.yaml', 'loon.conf')]
                for output in outputs:
                    g.secure_write(output, 'old output\n')
                with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit) as caught:
                    g.main(['--hosts-dir', str(root / 'hosts'),
                            '--plain', '--output', str(outputs[0]), '--raw-output', str(outputs[1]),
                            '--loon-output', str(outputs[2])])
                self.assertIn('clients', str(caught.exception))
                self.assertNotIn('SYNTHETIC_SECRET', str(caught.exception))
                for output in outputs:
                    self.assertEqual(output.read_text(), 'old output\n')

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


class LoonTests(unittest.TestCase):
    def test_loon_invalid_values_are_reported_as_skips(self):
        base = {'name': 'test', 'type': 'vless', 'server': 'node.example',
                'port': 443, 'uuid': UUID}
        for extra in ({'network': {}}, {'encryption': []}, {'tls': None},
                      {'sni': None}, {'sni': 'bad\nvalue'},
                      {'alpn': [{'invalid': True}]}, {'alpn': 123}, {'flow': 123},
                      {'reality-opts': {'public-key': 123}},
                      {'reality-opts': {'public-key': 'key', 'short-id': 123}}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                count, chain_count, skipped = g.write_loon([base | extra], Path(tmp) / 'loon.conf')
                self.assertEqual(count, 0)
                self.assertEqual(len(skipped), 1)

    def test_loon_plugin_empty_strings_preserved(self):
        body = g.loon_node_body({'type': 'ss', 'server': 'node.example', 'port': 443,
                                'cipher': 'aes-128-gcm', 'password': ' pass ',
                                'plugin': 'obfs', 'plugin-opts': {'mode': 'http', 'path': ''}})
        self.assertIn('obfs-uri=', body)

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
