# Clash Verge Rev home configuration

This repository contains a reusable Mihomo/Clash Verge Rev configuration template and a local node generator.

## Files

- `home.yaml`: main configuration template. Its `proxies` list is intentionally empty.
- `generate_raw_nodes.py`: reads private `vps-*` directories and generates a Clash Verge Rev YAML extension.

## Requirements

- Python 3.10+
- PyYAML (`python3 -m pip install PyYAML`)

## Usage

When this repository is a direct child of the private `hosts` directory, the generator automatically detects `vps-*` in the parent directory:

```bash
./generate_raw_nodes.py
```

You can also specify the private input directory explicitly:

```bash
./generate_raw_nodes.py --hosts-dir /path/to/private/hosts --interactive
```

The default interactive run creates:

- `clash-vps.generated.yaml`: paste into the Clash Verge Rev YAML extension; it uses `proxies:` field override.
- `nodes.yaml`: real base nodes only.

Both generated files contain live credentials and are excluded by `.gitignore`.

## Security

Never commit `vps-*`, `host.env`, private keys, Xray/Hysteria service configuration, `nodes.yaml`, or `clash-vps.generated.yaml`. If credentials are pushed accidentally, remove them from Git history and rotate them immediately.

