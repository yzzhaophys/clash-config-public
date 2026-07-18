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
- `nodes.yaml`: selected base nodes plus HomeIP/ShowIP aliases, without generated proxy chains or regional placeholders.

Both generated files contain live credentials and are excluded by `.gitignore`.

## Optional private airport subscription

Place a complete private subscription outside this repository:

```text
hosts/
├── airport/
│   └── subscription.yaml
├── vps-*/
└── clash-config-public/
```

On an interactive run, the first prompt asks whether to import it. The generator can filter by region, select individual nodes, and save only selected node names in `airport/selected-nodes.yaml`. Imported airport nodes remain independently selectable and never participate in proxy chains. Matching airport DNS policies are reported but are not written into the Merge output; add them manually to the existing `dns` section in `home.yaml` to avoid replacing that configuration.

HomeIP and ShowIP each support selecting multiple source nodes. Each selected source is replaced by dedicated HomeIP/ShowIP aliases and is completely removed from generated `proxies:` output. Self-hosted aliases can act as chain landing nodes. Airport HomeIP/ShowIP aliases remain independently selectable but never participate in proxy chains.

Airport-backed aliases retain both a stable source identifier and the original airport display name, for example `[Source=Airport.US.SS.00|Name=US Airport Node]`. Server addresses and credentials are never added to this label.

The subscription and saved selection remain outside this Git repository.

## Security

Never commit `vps-*`, `host.env`, private keys, Xray/Hysteria service configuration, `nodes.yaml`, or `clash-vps.generated.yaml`. If credentials are pushed accidentally, remove them from Git history and rotate them immediately.
