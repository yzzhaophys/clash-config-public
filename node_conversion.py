"""Explicit server-to-client mappings. Unknown client requirements fail closed.

Already-client-side Clash inventories are copied by the generator instead of
being passed through these server configuration converters.
"""

import copy
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit


def mapping(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f'{field} 必须是映射')
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f'{field} 的字段名必须是字符串')
    return value


def audit_fields(data: dict, converted: set, server_only: set, field: str,
                 audit: dict) -> None:
    unknown = set(data) - converted - server_only
    if unknown:
        raise ValueError(
            f'{field}: 暂不支持转换字段 {", ".join(sorted(unknown))}；'
            '请通过客户端 inventory 提供完整 Clash 参数'
        )
    audit['converted'].extend(f'{field}.{k}' for k in sorted(set(data) & converted))
    audit['server-only'].extend(f'{field}.{k}' for k in sorted(set(data) & server_only))


def copy_fields(source: dict, target: dict, names: dict) -> None:
    for key, renamed in names.items():
        if key in source:
            target[renamed] = copy.deepcopy(source[key])


def string(value: Any, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value) or '\n' in value or '\r' in value:
        raise ValueError(f'{field} 必须是有效字符串')
    return value


def xray_options(inbound: dict, client: dict, field: str) -> dict:
    audit = {'converted': [], 'server-only': [], 'derived': []}
    audit_fields(inbound, {'protocol', 'port', 'settings', 'streamSettings'},
                 {'tag', 'listen', 'sniffing', 'allocate'}, field, audit)
    settings = mapping(inbound.get('settings', {}), f'{field}.settings')
    audit_fields(settings, {'clients', 'decryption'}, {'fallbacks'}, f'{field}.settings', audit)
    if settings.get('decryption') not in (None, '', 'none'):
        raise ValueError(f'{field}.settings.decryption: 加密客户端参数需由 inventory 明确提供')
    audit_fields(client, {'id', 'flow', 'encryption'}, {'email', 'level'}, f'{field}.client', audit)
    out = {}
    copy_fields(client, out, {'flow': 'flow', 'encryption': 'encryption'})
    stream = mapping(inbound.get('streamSettings', {}), f'{field}.streamSettings')
    audit_fields(stream, {'network', 'security', 'tlsSettings', 'realitySettings',
                         'tcpSettings', 'rawSettings', 'wsSettings', 'grpcSettings', 'httpSettings'},
                 {'sockopt'}, f'{field}.streamSettings', audit)
    raw_network = stream.get('network', 'tcp')
    if not isinstance(raw_network, str):
        raise ValueError(f'{field}.network 必须是字符串')
    network = {'raw': 'tcp', 'websocket': 'ws', 'http': 'h2'}.get(raw_network.lower(), raw_network.lower())
    if network not in {'tcp', 'ws', 'grpc', 'h2'}:
        raise ValueError(f'{field}.network 暂不支持转换；请使用客户端 inventory')
    if 'network' in stream:
        out['network'] = network
    expected_settings = {'tcp': {'tcpSettings', 'rawSettings'}, 'ws': {'wsSettings'},
                         'grpc': {'grpcSettings'}, 'h2': {'httpSettings'}}[network]
    for key in {'tcpSettings', 'rawSettings', 'wsSettings', 'grpcSettings', 'httpSettings'} & set(stream):
        if key not in expected_settings:
            raise ValueError(f'{field}.{key} 与 network 不一致，不能静默忽略')

    if network == 'tcp':
        for key in expected_settings & set(stream):
            opts = mapping(stream[key], f'{field}.{key}')
            audit_fields(opts, {'header', 'acceptProxyProtocol'}, set(), f'{field}.{key}', audit)
            if opts.get('acceptProxyProtocol', False) is not False:
                raise ValueError(f'{field}.{key}.acceptProxyProtocol 需要显式客户端 inventory')
            if 'header' in opts:
                header = mapping(opts['header'], f'{field}.{key}.header')
                if set(header) - {'type'} or header.get('type', 'none') != 'none':
                    raise ValueError(f'{field}.{key}.header 暂不支持转换；请使用客户端 inventory')
    elif network == 'ws':
        ws = mapping(stream.get('wsSettings', {}), f'{field}.wsSettings')
        audit_fields(ws, {'path', 'headers', 'host', 'maxEarlyData', 'earlyDataHeaderName', 'acceptProxyProtocol'},
                     set(), f'{field}.wsSettings', audit)
        if ws.get('acceptProxyProtocol', False) is not False:
            raise ValueError(f'{field}.wsSettings.acceptProxyProtocol 需要显式客户端 inventory')
        opts = {}
        copy_fields(ws, opts, {'path': 'path', 'headers': 'headers',
                              'maxEarlyData': 'max-early-data', 'earlyDataHeaderName': 'early-data-header-name'})
        if 'headers' in opts:
            mapping(opts['headers'], f'{field}.wsSettings.headers')
        if 'host' in ws:
            headers = opts.setdefault('headers', {})
            existing = next((v for k, v in headers.items() if k.lower() == 'host'), None)
            if existing is not None and existing != ws['host']:
                raise ValueError(f'{field}.wsSettings.host 与 headers.Host 冲突')
            for k in list(headers):
                if k.lower() == 'host':
                    del headers[k]
            headers['Host'] = copy.deepcopy(ws['host'])
        if 'path' in opts:
            path = string(opts['path'], f'{field}.wsSettings.path', empty=True)
            parsed = urlsplit(path)
            query = parse_qsl(parsed.query, keep_blank_values=True)
            early = [v for k, v in query if k == 'ed']
            if early:
                if len(early) != 1 or not early[0].isdigit():
                    raise ValueError(f'{field}.wsSettings.path 的 ed 参数无效')
                value = int(early[0])
                if 'max-early-data' in opts and opts['max-early-data'] != value:
                    raise ValueError(f'{field}.wsSettings 的 early-data 参数冲突')
                opts['max-early-data'] = value
                if value:
                    opts.setdefault('early-data-header-name', 'Sec-WebSocket-Protocol')
                    audit['derived'].append('ws early-data header from Xray ed protocol')
                # Preserve the spelling/encoding of unrelated query parameters.
                remaining = [part for part in parsed.query.split('&')
                             if not any(k == 'ed' for k, _ in parse_qsl(part, keep_blank_values=True))]
                opts['path'] = urlunsplit(parsed._replace(query='&'.join(remaining)))
        if opts or 'wsSettings' in stream:
            out['ws-opts'] = opts
    elif network == 'grpc':
        grpc = mapping(stream.get('grpcSettings', {}), f'{field}.grpcSettings')
        audit_fields(grpc, {'serviceName'}, set(), f'{field}.grpcSettings', audit)
        opts = {}
        copy_fields(grpc, opts, {'serviceName': 'grpc-service-name'})
        if opts or 'grpcSettings' in stream:
            out['grpc-opts'] = opts
    elif network == 'h2':
        http = mapping(stream.get('httpSettings', {}), f'{field}.httpSettings')
        audit_fields(http, {'host', 'path'}, set(), f'{field}.httpSettings', audit)
        opts = {}
        copy_fields(http, opts, {'host': 'host', 'path': 'path'})
        if opts or 'httpSettings' in stream:
            out['h2-opts'] = opts

    security = stream.get('security', 'none')
    if not isinstance(security, str) or security not in {'none', 'tls', 'reality', ''}:
        raise ValueError(f'{field}.security 暂不支持转换；请使用客户端 inventory')
    for key, expected in [('tlsSettings', 'tls'), ('realitySettings', 'reality')]:
        if key in stream and security != expected:
            raise ValueError(f'{field}.{key} 与 security 不一致，不能静默忽略')
    if 'security' in stream:
        out['tls'] = security in {'tls', 'reality'}
    if security == 'tls':
        tls = mapping(stream.get('tlsSettings', {}), f'{field}.tlsSettings')
        audit_fields(tls, {'serverName', 'alpn', 'allowInsecure', 'fingerprint'},
                     {'certificates', 'rejectUnknownSni'}, f'{field}.tlsSettings', audit)
        copy_fields(tls, out, {'serverName': 'servername', 'alpn': 'alpn',
                              'allowInsecure': 'skip-cert-verify', 'fingerprint': 'client-fingerprint'})
    elif security == 'reality':
        reality = mapping(stream.get('realitySettings', {}), f'{field}.realitySettings')
        audit_fields(reality, {'publicKey', 'shortIds', 'serverNames', 'fingerprint'},
                     {'privateKey', 'show', 'dest', 'target', 'xver', 'minClientVer',
                      'maxClientVer', 'maxTimeDiff', 'limitFallbackUpload', 'limitFallbackDownload'},
                     f'{field}.realitySettings', audit)
        public = string(reality.get('publicKey'), f'{field}.realitySettings.publicKey 缺失；请提供客户端 inventory，私钥不会导出')
        opts = {'public-key': public}
        for source, target in [('shortIds', 'short-id'), ('serverNames', 'servername')]:
            if source in reality:
                choices = reality[source]
                if not isinstance(choices, list) or not choices or any(not isinstance(v, str) for v in choices):
                    raise ValueError(f'{field}.realitySettings.{source} 必须是非空字符串列表')
                # A client sends one accepted ID/SNI, not the server's whole list.
                (opts if source == 'shortIds' else out)[target] = choices[0]
                audit['derived'].append(f'reality {source}: first accepted value of {len(choices)}')
        out['reality-opts'] = opts
        copy_fields(reality, out, {'fingerprint': 'client-fingerprint'})
    out['_conversion-audit'] = audit
    return out


def hysteria_options(data: dict, field: str) -> dict:
    audit = {'converted': [], 'server-only': [], 'derived': []}
    audit_fields(data, {'listen', 'tls', 'auth', 'obfs'},
                 {'acme', 'masquerade', 'quic', 'bandwidth', 'ignoreClientBandwidth',
                  'disableUDP', 'udpIdleTimeout', 'resolver', 'acl', 'outbounds',
                  'trafficStats', 'speedTest'}, field, audit)
    auth = mapping(data.get('auth', {}), f'{field}.auth')
    audit_fields(auth, {'type', 'password'}, set(), f'{field}.auth', audit)
    if auth.get('type', 'password') != 'password':
        raise ValueError(f'{field}.auth 认证方式需由客户端 inventory 提供对应凭据')
    out = {'password': string(auth.get('password'), f'{field}.auth.password')}
    tls = mapping(data.get('tls', {}), f'{field}.tls')
    audit_fields(tls, {'sni'}, {'cert', 'key', 'sniGuard'}, f'{field}.tls', audit)
    copy_fields(tls, out, {'sni': 'sni'})
    if 'obfs' in data:
        obfs = mapping(data['obfs'], f'{field}.obfs')
        audit_fields(obfs, {'type', 'salamander'}, set(), f'{field}.obfs', audit)
        if obfs.get('type') != 'salamander':
            raise ValueError(f'{field}.obfs.type 暂不支持转换；请使用客户端 inventory')
        salamander = mapping(obfs.get('salamander', {}), f'{field}.obfs.salamander')
        audit_fields(salamander, {'password'}, set(), f'{field}.obfs.salamander', audit)
        out['obfs'] = 'salamander'
        out['obfs-password'] = string(salamander.get('password'), f'{field}.obfs.salamander.password')
    out['_conversion-audit'] = audit
    return out
