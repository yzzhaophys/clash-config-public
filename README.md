# Clash Verge Rev home configuration

可复用的 Mihomo/Clash Verge Rev 配置模板，以及用于生成自建节点和代理链的脚本。

## 文件

- `home.yaml`：主配置模板。它负责策略组和分流规则，`proxies` 列表保持为空。
- `generate_raw_nodes.py`：读取私有节点配置，生成基础节点和可选的代理链。

生成器负责生成基础节点和可选代理链；`home.yaml` 通过节点名称中的地区、角色和能力标记筛选节点。

## 要求

- Python 3.10+
- PyYAML：`python3 -m pip install PyYAML`

## 快速使用

默认运行会进入交互模式：

```bash
./generate_raw_nodes.py
```

也可以显式指定私有输入：

```bash
./generate_raw_nodes.py \
  --hosts-dir /path/to/private/hosts \
  --airport-dir /path/to/private/airport \
  --trusted-nodes-file /path/to/private/trusted-nodes.yaml \
  --interactive
```

常用选项：

- `--plain`：只输出基础节点，不生成代理链。
- `--template` / `--merge`：输出 `proxies` 模板和代理链；两个选项当前同义，保留两个名称兼容既有调用。
- `--chains all|none`：非交互模式下生成或跳过代理链；指定该选项时默认使用模板格式。
- `--routes 'HK<-JP,US<-HK'`：只生成指定的“最终出口 <- 中转入口”方向，并默认使用模板格式；HomeIP 可写成 `US.HomeIP<-JP`，大小写不敏感。
- `--exclude-node REGEX`：按节点名称排除基础节点，相关代理链也会被排除。
- `--raw-output PATH`：额外输出仅含基础节点的 YAML。
- `--loon-output PATH` / `--no-loon`：指定 Loon 输出文件，或关闭 Loon 输出。

默认目录和环境变量：

- 自建节点：`~/.config/infra/hosts`，可用 `CLASH_HOSTS_DIR` 覆盖。
- 机场/可信节点：`~/.config/clash/airport`，可用 `CLASH_AIRPORT_DIR` 覆盖。
- 可信节点文件：`trusted-nodes.yaml`，可用 `CLASH_TRUSTED_NODES_FILE` 覆盖。
- Ansible 角色配置：相邻 `infra/ansible/host_vars`，可用
  `CLASH_ANSIBLE_HOST_VARS_DIR` 或 `--ansible-host-vars-dir` 覆盖。

只有包含有效 `host.env` 的 `vps-*` 目录会被读取。Ansible `host_vars` 中的
`vps_clash_*` 值优先于 `host.env`；后者只是兼容旧配置。NAT 或非标准主机可用
`vps-*/secrets/client/clash-nodes.yaml` 声明面向客户端的地址和端口，该文件存在时
作为该主机唯一来源（即使其中 `proxies: []` 也不会回退到服务端配置）。
`VPS_CLASH_ORDER` 可用于稳定多个主机的排序和节点编号，必须是整数。

## 节点与代理链规则

### 能力开关

自建节点在 Ansible `host_vars/<hostname>.yml` 中配置；`vps_clash_region` 使用实际两位
地区代码，不要填写 `EUR` 等虚拟分组：

```yaml
vps_clash_region: jp
vps_clash_exit_type: general       # general 或 homeip
vps_clash_allow_relay: true        # 能否作为代理链第一跳/中转
vps_clash_allow_direct_exit: true  # 能否作为单节点最终出口
vps_clash_allow_chain_exit: true   # 能否作为代理链最终落地节点
vps_clash_allow_showip: false
vps_clash_allow_download: false
vps_clash_relay_protocol: vless
vps_clash_chain_exit_protocol: vless
```

这些能力相互独立：

- `allow_direct_exit: false`：节点名增加 `[Direct=false]`，不会进入 `DirectExit` 组，
  但不影响它参与代理链。
- `allow_relay: true`：节点可以作为第一跳/中转节点。
- `allow_chain_exit: true`：节点可以作为第二跳/最终出口。
- `allow_showip: true`：节点名增加 `[ShowIP=true]`，进入对应的 ShowIP 节点组和代理链。
- `allow_download: true`：节点名增加 `[Download=true]`，可进入下载专用节点组。

默认值：自建节点的 `allow_direct_exit` 和 `allow_chain_exit` 为 `true`，
`allow_showip` 和 `allow_download` 为 `false`。普通 `general` 节点只有 HK、JP、SG
默认开启 `allow_relay`；其他地区默认关闭。

`exit_type` 决定节点角色：

- `homeip` → `HomeIP`，不能开启 `allow_relay`；
- `general` 且 `allow_relay: true` → `Core`；
- `general` 且 `allow_relay: false` → `Exit`。

因此，`Core` 不等于只能中转；只要 `allow_direct_exit: true`，它也可以单节点直出。

这里的“直出/直连”是指“客户端只经过一个代理节点并由它作为最终出口”，不是
Clash 的 `DIRECT`（完全不经过代理）。

### 代理链生成

代理链的路径是：

```text
客户端 → Relay/中转节点 → Chain Exit/最终落地节点
```

只有同时满足以下条件才会生成：

- 最终落地节点允许 `allow_chain_exit`；
- 中转节点允许 `allow_relay`；
- 节点实际协议与相应的 relay/chain-exit 协议一致；自建节点的链路协议默认是 VLESS，
  可信节点未显式配置时默认使用其 `proxy.type`；
- 两个节点不能来自同一个物理节点；
- 普通出口不生成同地区代理链；HomeIP 允许不同物理节点的同地区落地例外；
- 地区补位节点不参与代理链。

`allow_direct_exit` 不参与代理链资格判断。因此带 `[Direct=false]` 的节点仍可能是
代理链的中转节点或最终落地节点。

### 机场和可信节点

机场订阅目录中的 `subscription.yaml` 需要在交互运行时选择节点，选择结果可保存到
`selected-nodes.yaml`。节点名称末尾需要有两位地区代码（例如 `... US`）；导入后的
机场节点固定为：允许单节点直出、不允许中转、不允许作为代理链落地，也不会被标记为
`HomeIP` 或 `ShowIP`。订阅中匹配到的 `dns.nameserver-policy` 只会报告，不会自动改写
`home.yaml`，避免生成器覆盖主配置的 DNS 策略。

可信节点放在私有 `trusted-nodes.yaml` 中：

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

可信节点的 `allow-relay` 和 `allow-chain-exit` 默认是 `false`，`allow-direct-exit`
默认是 `true`。必须填写稳定的 `id` 和实际两位国家代码；可信节点不能声明
`HomeIP`、`ShowIP` 或已有 `dialer-proxy` 链。稳定的 `id` 也用于禁止同一物理节点自连。

### 策略组筛选

`home.yaml` 中的规则与节点名称标记对应：

- `DirectExit` 组筛选基础节点，并排除 `PrxChain` 和 `[Direct=false]`；
- `Chain` 组只筛选 `PrxChain-*`；
- `ShowIP` 组筛选 `[ShowIP=true]`；
- 下载组筛选 `[Download=true]`，并排除 HomeIP、ShowIP、代理链和 `[Direct=false]`。

地区 `Line` 组再根据 `home.yaml` 的定义组合 `DirectExit` 和 `Chain`。因此修改
节点能力后，需要重新运行生成器并重新加载生成的配置。

## 输出文件

默认交互运行会生成：

- `clash-vps.generated.yaml`：Clash Verge Rev YAML 扩展配置，包含基础节点和选中的代理链；
- `nodes.yaml`：选中的基础节点，不含代理链和地区补位节点；
- `loon-nodes.conf`：选中的基础节点的 Loon 格式，不含 Clash 专用代理链和地区补位节点。

显式 `--plain` 时主输出默认为 `nodes.yaml`；`--template`、`--merge`、`--chains` 或
`--routes` 时主输出默认为 `clash-vps.generated.yaml`。显式指定的输出路径不能相同。
除非使用 `--no-loon`，每次运行都会额外生成 Loon 文件；当前仅转换 VLESS、Hysteria2
和 Shadowsocks，其他协议会跳过并在终端列出。交互模式还可以排除基础节点、选择代理链
方向或逐条选择代理链；被排除节点的相关代理链不会生成。

这些文件包含真实凭据，已加入 `.gitignore`。

## 编辑和安全

`[Direct=false]`、`[ShowIP=true]` 等标记是生成器根据源配置自动写入的，不是 Clash
节点编辑界面的标准字段。应修改 `host_vars` 或 `trusted-nodes.yaml` 后重新生成，
不要只在客户端手动改节点名称。

不要提交 `vps-*`、`host.env`、私钥、Xray/Hysteria 配置、`nodes.yaml`、
`loon-nodes.conf` 或 `clash-vps.generated.yaml`。凭据一旦泄露，应立即轮换。

`home.yaml` 的策略组格式需要保持现有的对齐风格；编辑代理组时不要重写 `dns`、
`rules` 或 `rule-providers`。

## 维护与验证

生成器的规则以本 README 和测试为准。修改命名、能力开关、代理链或输出格式后运行：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile generate_raw_nodes.py
```
