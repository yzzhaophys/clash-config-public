# Clash Verge Rev home configuration

This repository contains a reusable Mihomo/Clash Verge Rev configuration template and a local node generator.

## Files

- `home.yaml`: main configuration template. Its `proxies` list is intentionally empty.
- `generate_raw_nodes.py`: reads private `vps-*` directories and generates a Clash Verge Rev YAML extension.

For NAT or otherwise non-standard hosts, an optional
`vps-*/secrets/client/clash-nodes.yaml` can provide the public client address and
ports. Declare that host's Clash role in Ansible `host_vars`; legacy
`VPS_CLASH_*` values in `host.env` are compatibility fallbacks only. This client
inventory is authoritative for the host's public endpoints, and its nodes
remain eligible for proxy chains.
Set the optional integer `VPS_CLASH_ORDER` when a new host must sort after
existing hosts so their generated node names and indexes remain stable.

## Requirements

- Python 3.10+
- PyYAML (`python3 -m pip install PyYAML`)

## Usage

With the recommended directory layout, the generator automatically reads VPS inputs from `~/.config/infra/hosts` and optional airport/trusted-node inputs from `~/.config/clash/airport`:

```bash
./generate_raw_nodes.py
```

You can also specify the private input directory explicitly:

```bash
./generate_raw_nodes.py \
  --hosts-dir /path/to/private/hosts \
  --airport-dir /path/to/private/airport \
  --trusted-nodes-file /path/to/private/trusted-nodes.yaml \
  --interactive
```

`CLASH_HOSTS_DIR`, `CLASH_AIRPORT_DIR`, and `CLASH_TRUSTED_NODES_FILE` provide equivalent persistent overrides. Command-line arguments take precedence. The legacy `hosts/airport` location remains a fallback when the standard airport directory does not exist.

Only `vps-*` directories containing an active `host.env` are included. Retired
directories that retain only `host.env.retired-*`, historical `host_vars`, or
secrets remain available for recovery but are skipped by the node generator.

Interactive selection displays the source `vps-*` directory after self-hosted
node names (and shows source directories for proxy-chain exit/entry nodes) to
make similar nodes easier to distinguish. These labels are display-only and
are never written into generated node or proxy-chain names.

The default interactive run creates:

- `clash-vps.generated.yaml`: paste into the Clash Verge Rev YAML extension; it uses `proxies:` field override.
- `nodes.yaml`: selected base nodes, without generated proxy chains or regional placeholders.

Both generated files contain live credentials and are excluded by `.gitignore`.

## Optional private airport subscription

Place a complete private subscription outside this repository and outside the host inventory:

```text
~/.config/
├── infra/
│   └── hosts/
│       └── vps-*/
└── clash/
    └── airport/
        ├── subscription.yaml
        ├── selected-nodes.yaml
        └── trusted-nodes.yaml
projects/
└── clash-config-public/
```

On an interactive run, the first prompt asks whether to import it. The generator can filter by region, select individual nodes, and save only selected node names in the configured airport directory's `selected-nodes.yaml`. Imported airport nodes remain ordinary direct-only exits: they never relay, never act as chain landings, and cannot be converted to HomeIP/ShowIP. Matching airport DNS policies are reported but are not written into the Merge output; add them manually to the existing `dns` section in `home.yaml` to avoid replacing that configuration.

## Optional private trusted relay nodes

If you have node information but do not manage the corresponding machines,
put explicitly trusted client-side nodes in
`~/.config/clash/airport/trusted-nodes.yaml` (or use
`--trusted-nodes-file`/`CLASH_TRUSTED_NODES_FILE`):

```yaml
nodes:
  - id: provider-jp-01
    name: Provider JP Relay 01
    region: JP
    allow-relay: true
    allow-chain-exit: true
    allow-direct-exit: true
    allow-download: false
    proxy:
      type: vless
      server: example.com
      port: 443
      uuid: replace-with-private-uuid
      encryption: none
      tls: true
      network: ws
      servername: example.com
```

Each entry requires a stable `id`, an actual two-letter country code such as
`DE` or `NL` (use the actual node country, not the virtual `EUR` group), and a
basic VLESS or Hysteria2 `proxy`. `allow-relay` means the node may be the first
hop; `allow-chain-exit` means it may be the chain's landing node. Both are
`false` by default, so adding a node never silently expands the proxy-chain
set. `allow-direct-exit` defaults to `true`, while `allow-download` defaults
to `false`.

The generator marks these entries with `[Trusted=...]` and uses `id` as their
physical-node identity, preventing any entry from chaining to another entry
with the same `id`. Trusted entries are always ordinary general exits:
`HomeIP`, `ShowIP`, and pre-existing `dialer-proxy` chains are rejected. The
file contains credentials and is ignored by Git; keep it outside this
repository and only mark nodes as trusted when the provider permits this use.

Self-hosted node capabilities are public infrastructure metadata and should be
declared in the matching Ansible `host_vars/<hostname>.yml`:

```yaml
vps_clash_region: jp
vps_clash_allow_relay: true
vps_clash_allow_direct_exit: true
vps_clash_allow_chain_exit: true
vps_clash_exit_type: general
vps_clash_allow_showip: false
vps_clash_allow_download: false
vps_clash_relay_protocol: vless
vps_clash_chain_exit_protocol: vless
```

`vps_clash_exit_type` accepts `general` or `homeip`. ShowIP is an independent
capability controlled by `vps_clash_allow_showip`, so one HomeIP node may also
serve ShowIP traffic without creating a duplicate proxy. General HK,
JP, and SG nodes default to `Core`; other general self-hosted nodes default to
`Exit`. Self-hosted nodes default to direct and chain landing capability.
Airport nodes are always direct-only. A generated node carrying
`[Direct=false]` is excluded from direct-exit pools, while `[Download=true]`
can be selected by the dedicated download group. Proxy chains reject only the
same physical source, so a JP Core may relay to a different JP HomeIP host.
Ordinary same-region chains are not generated. Both relay and chain-landing
protocols default to VLESS, while every available protocol remains in direct
exit pools.

The generator automatically reads the sibling
`infra/ansible/host_vars` directory. Override it with
`CLASH_ANSIBLE_HOST_VARS_DIR` or `--ansible-host-vars-dir`. Ansible values are
authoritative; legacy `VPS_CLASH_*` values in private `host.env` files are only
a compatibility fallback.

HomeIP and ShowIP are never assigned interactively. A self-hosted node enters
its country HomeIP pool when `vps_clash_exit_type: homeip`; it additionally
enters the country ShowIP pool when `vps_clash_allow_showip: true`. The same
physical node and proxy chain can therefore belong to both pools.

The capabilities are independent. `vps_clash_allow_direct_exit` controls
whether the physical node appears in direct pools,
`vps_clash_allow_chain_exit` controls whether it may be a chain landing, and
`vps_clash_allow_relay` controls whether it may dial another landing. Enabling
direct exit on a HomeIP node that also has ShowIP enabled makes that same node
available in both HomeIP and ShowIP direct pools. It does not create a second
proxy.

## Proxy-group routing model

`home.yaml` currently uses the following layers:

```text
business policy (select)
  -> selected country/region Line
    -> DirectExit or Chain pool
```

A `select` group is manual. `store-selected: true` preserves the last choice
between restarts, so a previously selected country or `DIRECT` can remain
selected until changed in Clash Verge Rev. There is currently no active
cross-country `Global` or `Manual` failover group: selecting `US.Line` stays in
the US line, and does not automatically jump to JP, SG, AU, or EUR.

Ordinary country Lines use `fallback` between their DirectExit and Chain
pools. The US/JP/SG HomeIP Lines use `select` so the direct or chain HomeIP
path can be chosen explicitly. ShowIP is only a capability tag: a tagged
HomeIP node and each of its generated chains can match both country-specific
HomeIP and ShowIP pools without duplication.

The active regional set is US, JP, SG, HK, AU, and EUR. EUR is the single
European exit and currently matches DE/NL nodes; the old TW, UK, DE, NL, and FR
Line definitions remain commented until those regions are needed. Generic
business groups may expose several regions, while capability- or
region-sensitive groups should only expose the regions that are valid for that
business.

Same-region chains are omitted for ordinary exits. The only same-region
exception is a different physical Core landing on HomeIP. Chain relay and
landing protocols are currently restricted to VLESS; Hysteria2 remains
available for direct exit.

## Current special-group defaults

- `GlobalDNS`: `select`, default `Low.Latency`; `DIRECT` remains a manual
  fallback for diagnosis. This group routes DNS queries, not ordinary traffic.
- `ChinaDNS`: `select`, default `REJECT` to keep the current DNS protection
  behavior; `DIRECT` is available only as a manual override.
- `GlobalNTP` and `ChinaNTP`: default `DIRECT`. NTP uses UDP/123 and should not
  be treated as a proxy bandwidth test.
- `HttpDNS` and `Hijacking`: default `REJECT-DROP`; use `DIRECT` only when
  diagnosing a compatibility problem.
- `CDN`: default `DIRECT`; `Low.Latency` is a manual alternative when a CDN
  route needs to leave through a proxy. The group does not automatically find
  the fastest CDN node.
- `Final`: default `Low.Latency`.
- `Speedtest`: manual `select`, default `DIRECT`, with country, HomeIP, ShowIP,
  AU, EUR, and helper lines available for comparison. The Apple success URL
  checks reachability and rough latency only; it does not measure download
  bandwidth or prove the residential identity of an exit.
- `Max.Traffic` is a dedicated download-capable pool selected by download and
  storage-related business groups. Its Apple health check is not a true
  maximum-bandwidth measurement.

## `home.yaml` formatting

Only `proxy-groups` is maintained as aligned one-line flow YAML. Fields and
entries inside `proxies: [ ... ]` are padded to aligned comma columns within
their structural section. Active groups and single-`#` commented groups follow
the same format; headings beginning with `##` or more are left as prose.
Proxy-group `icon` fields are intentionally omitted. Formatting this section
must not rewrite `dns`, `rules`, or `rule-providers`.

With the current self-hosted inventory, selecting every base node and every
route produces 16 self-hosted base nodes and 28 VLESS chains. Airport imports
increase the base-node count, and selecting only some route directions in the
interactive prompt intentionally produces fewer than 28 chains. Always check
the final `wrote ...` summary before loading the generated extension.

The subscription and saved selection remain outside this Git repository. `CLASH_HOSTS_DIR` and `CLASH_AIRPORT_DIR` can override both private input roots when relocating them.

## Security

Never commit `vps-*`, `host.env`, private keys, Xray/Hysteria service configuration, `nodes.yaml`, or `clash-vps.generated.yaml`. If credentials are pushed accidentally, remove them from Git history and rotate them immediately.
