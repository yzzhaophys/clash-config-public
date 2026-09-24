# Clash Verge Rev home configuration

可复用的 Mihomo/Clash Verge Rev 配置模板，以及用于生成自建节点和代理链的脚本。

## 文件职责

| 文件 | 来源与作用 | 是否含凭据、是否提交 |
| --- | --- | --- |
| `home.yaml` | 手工维护的 Mihomo/Clash Verge 主模板；定义 DNS、策略组、节点筛选和分流，`proxies` 为空 | 公开，提交 |
| `stash-dns-policy.yaml` | 手工维护的 Stash 专用 DNS policy；保存新增的整段规则及注释，独立于 `home.yaml` | 公开，提交 |
| `home-stash.yaml` | `generate_stash_config.py` 合并上述两个输入生成的 Stash 骨架；没有节点，也没有可在 Stash 运行时筛选节点的表达式 | 公开，提交；不能单独作为最终配置使用 |
| `clash-vps.generated.yaml` | `generate_raw_nodes.py` 输出的 `proxies` 片段，包含基础节点及按本次选择生成的代理链；供 Clash Verge 扩展配置及 Stash 私有生成器使用 | 含凭据，忽略、不提交 |
| `nodes.yaml` | 同一节点生成器输出的纯基础节点 `proxies` 列表，不含代理链 | 含凭据，忽略、不提交 |
| `loon-nodes.conf` | 同一节点生成器输出的 Loon 基础节点格式；不能表达的整条节点会被跳过 | 含凭据，忽略、不提交 |
| `home-stash.private.yaml` | `generate_stash_private.py` 将骨架、静态节点和 `home.yaml` 的筛选条件合并后生成；空组固定为 `REJECT` | 含凭据，忽略、不提交；这是导入 Stash 的文件 |
| `trusted-nodes.yaml` | `manage_trusted_nodes.py` 管理的私有 trusted inventory；也是节点生成器的输入，不是客户端配置 | 含凭据，忽略、不提交 |

| 脚本或目录 | 职责 |
| --- | --- |
| `generate_raw_nodes.py` | 读取自建 VPS、trusted inventory 和交互确认的机场节点，生成上述三种节点输出；模板模式还按 `home.yaml` 校验筛选 |
| `manage_trusted_nodes.py` | 查看、预览或应用 trusted inventory 的导入、删除、恢复；只改清单，不自动重新生成客户端文件 |
| `generate_stash_config.py` | 转换 `home.yaml`，合并 `stash-dns-policy.yaml`，生成 `home-stash.yaml` |
| `generate_stash_private.py` | 读取 `home-stash.yaml`、`clash-vps.generated.yaml`、`home.yaml`，生成 `home-stash.private.yaml`；拒绝过期骨架 |
| `node_io.py` | 脚本共用的严格 YAML 读取，拒绝显式重复键 |
| `node_conversion.py` | 服务端 Xray/Hysteria 参数到客户端节点字段的转换与审计辅助函数 |
| `tests/test_generate_raw_nodes.py`、`tests/test_manage_trusted_nodes.py` | 节点生成及 trusted inventory 管理回归测试 |
| `tests/test_node_parameter_safety.py`、`tests/test_trusted_launch.py` | 参数保留、文件安全、trusted 节点导入及输出测试 |
| `tests/test_proxy_group_policy.py` | `home.yaml` 策略组与筛选关系测试 |
| `tests/test_stash_config.py`、`tests/test_generate_stash_private.py` | Stash 骨架、固定 DNS 合并及私有配置生成测试 |
| `.gitignore` | 阻止私有输入、含凭据生成文件和缓存进入 Git |
| `AGENTS.md` | 仓库协作、安全和交付约束 |

私有输入通常在仓库外：`vps-*` 主机目录、Ansible `host_vars`、机场目录中的
`subscription.yaml` / `selected-nodes.yaml`，以及 trusted inventory。生成器负责
生成基础节点和可选代理链；`home.yaml` 通过节点名称中的地区、角色和能力标记筛选节点。

## 配置生产流程

```text
私有 VPS / trusted / 机场输入 ──generate_raw_nodes.py──► clash-vps.generated.yaml
                                                  ├──► nodes.yaml（可选纯节点输出）
                                                  └──► loon-nodes.conf（可选 Loon 输出）

home.yaml + stash-dns-policy.yaml ──generate_stash_config.py──► home-stash.yaml
home-stash.yaml + clash-vps.generated.yaml + home.yaml
                              ──generate_stash_private.py──► home-stash.private.yaml
```

Clash Verge 使用 `home.yaml` 的规则结构与节点生成器的 `proxies` 片段；这里的脚本
不会自动合并或重载正在运行的 Clash Verge 配置。Stash 只导入最后生成的
`home-stash.private.yaml`，其节点成员是生成时的静态快照；节点或任一上游模板改变后，
按下文的更新顺序重新生成并导入。Loon 使用单独生成的 `loon-nodes.conf`。

## 要求

- Python 3.10+
- PyYAML：`python3 -m pip install PyYAML`
- 节点管理脚本使用 `fcntl` 文件锁，在 Linux、macOS 或 WSL 中运行。

## 快速使用

以下命令从仓库目录执行；两个节点脚本均可用 `./脚本名.py` 或 `python3 脚本名.py` 运行。
生成器无参数运行时进入交互模式：

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
- `--home-template PATH`：指定模板模式校验所用的 `home.yaml`；生成器会校验组引用和实际节点/代理链筛选。
- `--raw-output PATH`：额外输出仅含基础节点的 YAML。
- `--loon-output PATH` / `--no-loon`：指定 Loon 输出文件，或关闭 Loon 输出。

### 生成 Stash 配置

先生成公开骨架：

```bash
python3 generate_stash_config.py
```

`stash-dns-policy.yaml` 独立保存七千多行 Stash 专用 `nameserver-policy` 及注释。
生成器检查它与 `home.yaml` 的条目不重名，再生成 `home-stash.yaml`。
骨架没有节点和运行时节点筛选，不能作为最终配置导入；不要直接编辑这个生成文件。

已有私有的 `clash-vps.generated.yaml` 后，生成供 Stash 导入的完整配置：

```bash
python3 generate_stash_private.py
```

输出 `home-stash.private.yaml` 含凭据、被 Git 忽略，权限为 `0600`。私有生成器会拒绝
与当前 `home.yaml` 或固定 DNS 输入不一致的旧骨架，并按 `home.yaml` 的筛选条件生成
静态组成员。空节点池改为只含 `REJECT` 的 `select` 组；上层自动组会移除已封闭的
空子组，没有可用出口时也封闭为 `REJECT`。Stash 3.4.1 已实测这种 `select`
组可显示 `REJECT`；公开骨架中的空自动组不能提供同样保证，参见
[Stash 策略组文档](https://stash.wiki/proxy-protocols/proxy-groups)。
私有生成器不接受动态 `proxy-providers`，节点变化后须重新生成并导入。

### Stash 转换边界

- 节点字段只做必要映射：Hysteria2 的 `password` → `auth`，VLESS 的
  `servername` → `sni`，其余客户端字段和值保留；认证冲突、代理链循环和筛选契约
  变化会报错。`DirectExit` 的成员在生成时计算，不把超长筛选正则交给 Stash。
- 源配置的 `empty-fallback: REJECT` 在公开骨架中省略，由私有静态组封闭空组。
  `PASS` 手选项被移除；`DIRECT`、`REJECT`、`REJECT-DROP` 保留。
- Stash 专用 DNS policy 由独立文件合并；Clash Meta 的 `rule-set:` DNS policy
  和 DNS URL 的代理组后缀不能等价迁移。`+.*` 转为默认 `nameserver`，避免
  通配规则遮蔽 `geosite:`。`follow-rule` 控制 DNS 请求的路由，不保证 DNS 结果
  与网站连接使用同一出口。[Stash DNS 文档](https://stash.wiki/features/dns-server)
- 当前骨架保留 `proxy-server-nameserver`，但独立解析功能要求 Stash iOS/tvOS
  3.6+ 或 macOS 4.3+；在 iOS 3.4.1 上不能依赖它防止代理服务器域名解析递归。
  Mihomo 的规则集下载 `proxy` 字段也会在 Stash 转换时移除，因此远端规则集不再
  有源配置指定的下载代理；下载失败需要单独检查，不代表代理节点失效。
- 单个节点的 Stash 测速地址和超时用 `benchmark-url`、`benchmark-timeout` 设置。
  `select` 组设置 `interval: -1`，自动组保留原模板的间隔和 `lazy` 设置。
  HTTP 测速成功不代表 UDP 可用；配置加载和本地字段比对也不能证明实际连通。

### Stash 配置更新顺序

`home.yaml` 是策略组、筛选和分流基础；七千多行 Stash 专用 DNS 规则由
`stash-dns-policy.yaml` 独立维护。`home-stash.yaml` 是两者合并生成的中间文件，
不要手动编辑。最终只将 `home-stash.private.yaml` 导入 Stash。

| 变更 | 重新生成 |
| --- | --- |
| 修改 `home.yaml` | 依次运行 `python3 generate_stash_config.py`、`python3 generate_stash_private.py` |
| 修改 `stash-dns-policy.yaml` | 依次运行 `python3 generate_stash_config.py`、`python3 generate_stash_private.py` |
| 直接修改私有节点文件 `clash-vps.generated.yaml` | 运行 `python3 generate_stash_private.py` |
| 通过 `manage_trusted_nodes.py` 修改 trusted inventory | 先运行 `generate_raw_nodes.py` 更新节点输出，再运行 `python3 generate_stash_private.py` |
| 修改其他生成节点所用的私有原始资料 | 先运行 `generate_raw_nodes.py` 更新节点文件，再运行 `python3 generate_stash_private.py` |

每次生成后都需要重新导入最终的私有文件，Stash 中已导入的配置不会自动同步工作区文件。
修改节点名称或筛选契约时，脚本可能要求重新审核转换；不要绕过报错或直接修改生成文件。
运行几天观察时，重点查看空组是否只显示 `REJECT`、常用国内外网站的连接记录和 DNS
查询记录，以及节点失效后的实际切换；配置加载或单元测试不能代替这些运行结果。

### 本地文件清理

`__pycache__/` 和 `tests/__pycache__/` 是可重新生成的 Python 缓存，可以清理。
`clash-vps.generated.yaml`、`home-stash.private.yaml`、`nodes.yaml`、`loon-nodes.conf`
以及 `trusted-nodes.yaml` 属于被 Git 忽略的私有输入或导出产物；不要把它们当作
垃圾删除，也不要提交。公开的 `stash-dns-policy.yaml` 是固定 DNS 输入，
`home-stash.yaml` 是生成私有 Stash 配置所需的中间文件，两者都应保留。

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

## 三种节点来源

| 来源 | 私有输入 | 导入时机 | 命名与标记 | 默认链路能力 |
| --- | --- | --- | --- | --- |
| 自建 VPS | `vps-*/host.env` 及 Xray/Hysteria 配置；NAT 主机可用 `vps-*/secrets/client/clash-nodes.yaml` | 每次运行自动导入 | `VPS-[US.Core]-...`、`VPS-[US.Exit]-...` 或 `VPS-[US.HomeIP]-...` | 直出和 Chain 落地默认开启；HK、JP、SG 的普通节点默认可 Relay，可由 `host_vars` 覆盖 |
| Trusted（未纳入主机管理的自建/客户端节点） | `trusted-nodes.yaml` (`nodes` 格式) | 每次运行自动导入 | `VPS-[US.Core|Exit|HomeIP]-...`；交互时显示来源文件 | 按 `exit-type` 和 `allow-*` 字段决定，默认仅允许直出 |
| 机场订阅 | `subscription.yaml` 和可选的 `selected-nodes.yaml` | 仅交互模式中确认导入 | `(...机场出口)-[Airport=...]` | 仅允许直出，不可 Relay，不可作为 Chain 落地 |

三种来源共享 `(region, protocol)` 编号计数器，按“自建 VPS → Trusted
→ 机场订阅”的顺序分配编号。因此已有一个美国 H2 自建节点时，后续的美国
Trusted H2 节点会使用 `H2-01`。物理节点身份分别由 VPS 目录名、
Trusted 的稳定 `id`/原始节点名和机场原始节点名确定，用于防止同一
物理节点自连。

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
- `allow_showip: true`：节点名增加 `[ShowIP=true]`。节点只有在同时允许
  `allow_direct_exit` 时才进入 ShowIP 直出组；即使禁止单节点直出，只要允许作为链路出口并满足组链条件，
  仍可进入 ShowIP 代理链组。
- `allow_download: true`：节点名增加 `[Download=true]`，可进入下载专用节点组。

默认值：自建节点的 `allow_direct_exit` 和 `allow_chain_exit` 为 `true`，
`allow_showip` 和 `allow_download` 为 `false`。普通 `general` 节点只有 HK、JP、SG
默认开启 `allow_relay`；其他地区默认关闭。

`exit_type` 决定自建 VPS 和 Trusted 节点的角色：

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
- 普通 Core/Exit、ShowIP 和 HomeIP 都允许使用不同物理节点生成同地区代理链。

ShowIP 是出口节点的附加能力标记，不是独立出口角色。节点仍然是普通 Exit 或
HomeIP；`[ShowIP=true]` 让节点可参与对应的 ShowIP 策略组。基础节点是否进入 ShowIP
直出组仍由 `allow_direct_exit` 决定；禁止单节点直出的节点仍可作为代理链落地节点进入 ShowIP 链组。

`allow_direct_exit` 不参与代理链资格判断。因此带 `[Direct=false]` 的节点仍可能是
代理链的中转节点或最终落地节点。

### 机场和可信节点

机场订阅目录中的 `subscription.yaml` 需要在交互运行时选择节点，选择结果可保存到
`selected-nodes.yaml`。节点名称末尾需要有两位地区代码（例如 `... US`）；导入后的
机场节点固定为：允许单节点直出、不允许中转、不允许作为代理链落地，也不会被标记为
`HomeIP` 或 `ShowIP`。订阅中匹配到的 `dns.nameserver-policy` 仅报告数量，不打印或导出
策略内容，也不会自动改写 `home.yaml`。需要时在私有订阅中核对所选节点域名对应的策略，
再手动合入主配置；包含私有域名或认证信息的策略不得提交到公开模板。

可信节点必须放在私有 `trusted-nodes.yaml` 的 `nodes` 列表中。每个节点都需要
显式指定稳定 `id`、实际两位国家代码和 `proxy`；`exit-type` 和能力由文件中的
字段控制，默认只允许单节点直出。Trusted 是未纳入主机管理的自建/客户端节点，
不是机场订阅节点；`allow-showip: true` 可用于已经核实实际公网出口地区的节点，
并与自建 VPS 使用相同的 ShowIP 标记和筛选。
生成名称与自建 VPS 保持一致，
使用 `VPS-[地区.角色]-协议-编号-(地区节点)` 格式，
并在交互选择时显示“来源文件: trusted-nodes.yaml”。脚本不再接受顶层
`proxies:`。

```yaml
nodes:
  - id: provider-us-01
    name: Provider US NAT 01
    region: US
    exit-type: general       # general 或 homeip
    allow-relay: false
    allow-chain-exit: false
    allow-direct-exit: true
    allow-download: false
    allow-showip: false
    proxy:
      type: vless
      server: example.com
      port: 443
      uuid: replace-with-private-uuid
```

需要让 Trusted 节点参与 Relay 或 Chain 时，在同一个 `nodes` 条目中将
`allow-relay` 或 `allow-chain-exit` 设为 `true`，并让对应的协议字段与
`proxy.type` 一致：

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

如果 Trusted 只作为代理链的最终落地节点，不作为中转或单节点直出，可使用：

```yaml
exit-type: general
allow-relay: false
allow-chain-exit: true
allow-direct-exit: false
allow-showip: false       # 需要作为 ShowIP 链路时改为 true
```

可信节点的 `allow-relay`、`allow-chain-exit`、`allow-showip` 和 `allow-download`
默认是 `false`，`allow-direct-exit` 默认是 `true`。`exit-type` 可设为 `general`
或 `homeip`；HomeIP 不能同时设置 `allow-relay: true`。必须填写稳定的 `id` 和
实际两位国家代码，且不能包含已有 `dialer-proxy` 链。只有确认节点实际公网出口
地区与 `region` 一致时，才应将 `allow-showip` 设为 `true`。稳定的 `id` 也用于
禁止同一物理节点自连。

多台 NAT 节点应合并到同一个 `trusted-nodes.yaml`，不要直接覆盖已有文件。使用仓库内的
`manage_trusted_nodes.py` 先预览、再按 `id + proxy.type` 应用：同一物理节点可以分别登记
VLESS、Hysteria2 和 SOCKS5；新组合会追加，已有组合原位更新，其他节点保留。应用时会创建 0600
时间戳备份并进行锁定和原子替换。判断节点是否变化时递归比较值及类型，
嵌套参数的 `false` → `0` 或整数 → 浮点数也算更新。
目标文件和包含凭据的临时源文件都必须禁止 group/other 访问；
源文件与目标文件不能指向同一个文件，包括硬链接别名。
日常维护运行：

```bash
./manage_trusted_nodes.py
```

菜单顶部显示实际管理的 inventory 路径。`--target` 可省略：优先使用
`CLASH_TRUSTED_NODES_FILE`，否则沿用生成器的机场目录（包括 `CLASH_AIRPORT_DIR`）
下的 `trusted-nodes.yaml`；显式 `--target` 优先。

| 选项 | 操作 |
| --- | --- |
| `1` 查看节点 | 显示编号、稳定 ID、地区、协议和能力，不显示地址或凭据 |
| `2` 删除节点 | 按编号选择；同 ID 有多个协议时，选择所选协议或全部协议 |
| `3` 导入节点 | 按编号选源 YAML，也可直接输入路径；预览新增、更新节点及变化字段 |
| `4` 恢复备份 | 按编号选择备份，预览新增、删除和更新，再完整恢复文档 |
| `0` 退出 | 退出菜单；各操作也可回车取消 |

写入前输入 `y` 确认。新增、更新通过完整的 `nodes:` YAML 导入，文件名不限，例如
`landing-jp-node.yaml`。相同 ID + 协议会用源条目完整替换，源文件中没有提到的节点保留；
不能选择目标文件自身，也不接受普通订阅的 `proxies:` 格式。

选项 `3` 默认列出目标 inventory 同目录下 `imports/` 中的 `.yaml`、`.yml` 文件，
不递归扫描。可用 `--import-dir` 或 `CLASH_TRUSTED_IMPORT_DIR` 指定已有的私有目录；
显式参数优先。目录不会自动创建或搬移文件；目录为空时仍可输入源路径。
符号链接和目标文件别名不列为候选，导入仍检查源文件权限，节点 YAML 应设置为 `0600`。
预览只显示 ID、协议和变化字段名（如 `proxy.password`、`proxy.ws-opts`），
不显示字段值或嵌套字典键。导入使用私有临时快照，退出后自动清理。

选项 `4` 列出目标同目录的 `文件名.bak-*` 备份及修改时间（UTC）。
恢复前会另存当前 inventory；目标不存在时直接恢复。备份必须通过权限、严格 YAML
和节点校验，原备份不会修改。删除、导入和恢复在确认及写入期间持有目标锁。
操作只修改 inventory，不会自动生成节点或重新加载客户端。

```bash
# 指定其他主清单，或指定存放待导入文件的目录
./manage_trusted_nodes.py --target /path/to/trusted-nodes.yaml
./manage_trusted_nodes.py --import-dir /path/to/private/landing-nodes

# 命令行模式：list 只读，merge/remove 默认预览，追加 --apply 才写入
./manage_trusted_nodes.py list
chmod 600 /path/to/landing-jp-node.yaml
./manage_trusted_nodes.py merge --source /path/to/landing-jp-node.yaml
./manage_trusted_nodes.py merge --source /path/to/landing-jp-node.yaml --apply
./manage_trusted_nodes.py remove --id provider-us-01
./manage_trusted_nodes.py remove --id provider-us-01 --apply
```

命令行 `remove` 省略 `--protocol` 会删除该 ID 的全部协议；
指定 `--protocol vless|hysteria2|socks5` 时只删除一个协议。
所有命令均可用 `--target` 指定主清单。

应用导入、删除或恢复后，使用所需选项重新运行生成器，校验候选配置后再加载客户端；
要重写三份输出，请显式提供输出路径；带参数的交互调用不会自动补充 `--raw-output`：

```bash
./generate_raw_nodes.py \
  --trusted-nodes-file ~/.config/clash/airport/trusted-nodes.yaml \
  --output clash-vps.generated.yaml \
  --raw-output nodes.yaml \
  --loon-output loon-nodes.conf \
  --interactive
```

退役后先在 `trusted-nodes.yaml` 中确认稳定 `id` 已删除。稳定 `id` 不会导出到
`nodes.yaml`、`clash-vps.generated.yaml` 或 `loon-nodes.conf`，不能靠搜索它检查生成产物。
应在私有环境中使用退役前记录的协议、连接地址、端口及凭据组合核对三份重新生成的输出，
同时检查基础节点和代理链；不要仅凭可能重新编号的名称或共享地址判定。
若其他事实源仍提供同一节点，需同步退役。另行确认客户端实际加载了新配置，
检查完备份后再按控制端保留策略处置临时源文件和过期备份；核对过程不得输出凭据。

SOCKS5 使用 `proxy.type: socks5`，必须配置 `username` 和 `password`，默认只作为直出节点；
它会进入 Clash/Mihomo 输出，Loon 输出也会转换已认证 SOCKS5，并保留可表达的 TLS、SNI、证书校验和 UDP 参数。

### 策略组筛选

`home.yaml` 中的规则与节点名称标记对应：

- `DirectExit` 组筛选基础节点，并排除 `PrxChain` 和 `[Direct=false]`；
- `Chain` 组只筛选 `PrxChain-*`；
- ShowIP 直出组筛选 `[ShowIP=true]`，并继续排除 `[Direct=false]`；ShowIP 链组筛选带 ShowIP 标记的代理链；
- 下载组只要求节点带 `[Download=true]`，另外排除名称中包含 `PrxChain` 的代理链；
  `HomeIP`、`ShowIP` 和 `[Direct=false]` 节点只要带有该下载标记也会进入 Download。

地区 `Line` 组再根据 `home.yaml` 的定义组合 `DirectExit` 和 `Chain`。因此修改
节点能力后，需要重新运行生成器并重新加载生成的配置。

### 代理组检测与故障切换

业务组使用 `select` 保留手选，实际节点选择交给其下的自动组。
`select` 不会阻止子组自行检测，也不会在子组失效后替用户选择另一个子组。
手选组保留测试 URL、状态码和超时信息，但移除 `interval`、`lazy`、
`max-failed-times`；显式设置周期原本可能产生检测流量，却不会使手选组自动切换。

| 层级 | 策略 | 检测安排与边界 |
| --- | --- | --- |
| 业务入口 | `select` | 手动选择线路；无额外周期检测 |
| `⬇️.Route-[Max.Traffic]` | `fallback` | 优先引用 Download，全部失效时回退 `♾️.Route-[Final.Fallback]` |
| Download | `url-test` | 每 30 秒检测获准下载的真实节点 |
| 其他 Chain / DirectExit（CN 除外） | `url-test` | 每 30 秒检测各自节点池 |
| 普通国家 Line（CN 除外） | `fallback` | 检测间隔 45 秒；US / JP / SG 代理链优先、同地区直出备用，其余地区直出优先、代理链备用 |
| 内层 HomeIP / ShowIP Line | `fallback` | 检测间隔 45 秒，代理链优先、同地区同用途直出备用 |
| 地区 Route、Final.Fallback、Low.Latency | `fallback` | 每 90 秒检测候选线路 |
| CDN 业务入口 | `select` | 默认引用 Max.Traffic，也可手选 Low.Latency；不增加跨组自动灾备层 |
| Americas / Oceania / Europe Route | `fallback` | 每 90 秒检测；本地区线路优先，`♾️.Route-[Final.Fallback]` 作为跨地区备用 |
| CN Line / CN DirectExit | 单子项 `select` | 当前最终指向 DIRECT，不提供回国代理节点或自动灾备 |

业务组通过 `🏠.Route-[US.HomeIP.Preferred]`、`📍.Route-[US.ShowIP.Preferred]` 等上层
Route 入口选择用途；每 90 秒检查一次，先使用对应的内层地区 Line，失效时回退到同地区普通
Line。内层 HomeIP / ShowIP 线路仍只在各自的 Chain 和 DirectExit 组之间切换。

所有自动组设置 `lazy: true`、`max-failed-times: 2`；`url-test` 节点池使用
`timeout: 3000`，普通国家线路 `fallback` 使用 `timeout: 4000`，HomeIP / ShowIP、跨地区及入口线路
使用 `timeout: 5000`。
HK 和 MY 的 Chain 子组在客户端列表中隐藏，仍由对应地区的 Line 组引用。
`url-test` 使用 `tolerance: 50` 毫秒，减少健康节点间的小幅延迟切换；
该容差不会阻止内核替换已被探测判定失效的节点。失败阈值只用于触发额外检查，
其计数受内核版本、失败类型及时间窗口影响，不保证两次业务请求失败就换线。
检测周期也不是故障恢复时限：探测耗时、多层状态更新、实际流量与探测流量的差异
都会影响恢复。底层检测更频繁，会增加后台探测开销。

`☁️.<Global>--CDN` 的首选项为 `⬇️.Route-[Max.Traffic]`，保留
`⚡.Route-[Low.Latency]`、DIRECT 等手动选项。
配置启用了 `store-selected`，已有选择可能优先于列表首项。CDN 不会在 Download
整体失效时自动改选 Low.Latency 或 DIRECT，需要手动选择；Download 内部仍自主换节点。
下载业务仍走 `⬇️.Route-[Max.Traffic]`；它优先使用 Download 节点池，Download 全部故障时明确
回退到 `♾️.Route-[Final.Fallback]`，因此该灾备路径可能使用未带 `allow_download` 标记的普通节点。
`home.yaml` 的 `empty-fallback: REJECT` 只处理节点池为空，不等于全部节点测速失败时的跨组灾备；
公开的 Stash 骨架无法保留这一空组回退，需使用上面的私有静态生成步骤将空组固定为 `REJECT`。
缺失地区不会生成占位节点。地区 `fallback` 只有在
备用节点池实际包含可用节点时才具有备用路径；两个池均为空的地区线路不能使用。

当前统一探测 Apple 测试页面并要求 HTTP 200，它只能代表该地址可达，不能证明
linux.do、其他站点或 UDP 正常，也不衡量下载带宽。上层探测一个子组时，检验的
是子组当时选中的路径，不能替代底层节点池的独立检测；不能承诺嵌套后瞬时恢复。
底层保留独立检测配置，实际调度受 `lazy: true` 影响。单子项 `select` 入口不再重复定时探测。Americas / Oceania /
Europe Route 的 `fallback` 会按列表顺序使用本地区线路，全部本地区候选失效后才尝试
`♾️.Route-[Final.Fallback]`；它关注可用性，不按延迟重新排序。

加载候选配置后应分别验证：

1. 查看 Download 的实际成员，确认各成员符合私有清单中的下载能力声明及名称筛选；
   自动组保持自动选择，检查面板/API 是否存在手动固定的 `fixed` 状态。
2. 在隔离测试环境使当前节点失效，观察组内检测历史和 `now` 是否变化，
   再用新连接访问目标站点；旧的 TCP/下载连接不能自动迁移，应用需要重连。
3. 测试 Download 全部失效时，Max.Traffic 是否按 Download → `♾️.Route-[Final.Fallback]` 回退，
   CDN 是否保持手选；有实际备用节点时，测试代理链故障后同用途直出是否接替。
4. 如果手动测速后仍不切换，确认测的是整个自动组而非仅一个节点或外层入口，
   核对自动组测试 URL 对应的结果、候选节点健康状态和 `fixed`。
   目标站点失败而 Apple 正常时，缩短检测周期不能解决，需要单独诊断站点路径。

修改模板不等于修改已生成或正在生效的配置。更新时需用自己的节点输入生成候选配置，
通过对应版本 Mihomo 的 `-t` 后，再按客户端工作流加载；配置加载成功不代表灾备实测通过。

## 输出文件

默认交互运行会生成：

- `clash-vps.generated.yaml`：Clash Verge Rev YAML 扩展配置，包含基础节点和选中的代理链；
- `nodes.yaml`：选中的基础节点，不含代理链；
- `loon-nodes.conf`：选中的基础节点的 Loon 格式，不含 Clash 专用代理链。

显式 `--plain` 时主输出默认为 `nodes.yaml`；`--template`、`--merge`、`--chains` 或
`--routes` 时主输出默认为 `clash-vps.generated.yaml`。显式指定的输出路径不能相同。
除非使用 `--no-loon`，每次运行都会额外生成 Loon 文件；当前转换 VLESS、Hysteria2、
Shadowsocks 和已认证 SOCKS5，其他协议会跳过并在终端列出。交互模式还可以排除基础节点、选择代理链
方向或逐条选择代理链；被排除节点的相关代理链不会生成。

模板模式会在写入私有输出前读取 `home.yaml`，检查策略组引用没有断链，并用实际生成的
节点名和代理链名匹配 `DirectExit`、`Download`、`ShowIP`、`Chain` 筛选表达式。
筛选不一致时会在替换任何输出前失败；`--plain` 只生成基础节点，因此跳过这项模板校验。

这些文件包含真实凭据，已加入 `.gitignore`。

### 参数保留与转换限制

- 客户端 Clash inventory 的节点参数按原结构保留，包括未知扩展字段、嵌套字段、
  `false`、`0` 和空值；内部能力及审计字段不写入节点配置。
- 从服务端配置转换时，只转换明确支持的客户端参数。缺失的可选参数不补造；
  缺少必要地址、端口或凭据会报错。VLESS 入站的 `settings.clients` 必须是非空列表，
  缺失、空值或非法类型会中止生成，即使还有其他有效节点也不会输出缺节点的配置。
  有值但无法可靠转换的字段也会报错，
  此时请在客户端 inventory 中提供完整 Clash 节点，不要删除参数来绕过检查。
- Shadowrocket 原始节点 JSON 不属于这里的 Clash inventory；不能只套一层 `nodes`。
  转换 VLESS 时必须核对原导出的 `password` 认证值与节点记录 `uuid`，不能按同名字段猜测。
  转换前保留私密原件；结构和内核加载通过后仍需真实代理链请求验收。
- Xray 支持 TCP、WS、gRPC、H2 的显式参数映射；WS 保留 path、Host、headers
  和 early-data。REALITY 必须提供客户端公钥，不会把服务端私钥写入输出。
  每个 inbound 继续选择第一个客户端账号；多个允许的 REALITY SNI / short-id
  选择第一项。这些选择都会记录在审计中。
- Hysteria2 保留 Salamander 混淆类型与密码。连接地址来自 `VPS_HOST`，与 TLS SNI
  分开处理；未显式提供 SNI 时，可从已有证书的 `live/<域名>/` 路径推导 SNI，
  该推导会记入审计。用户名、密码的首尾空格原样保留，换行和非法类型会被拒绝。
- 添加 `--audit` 可查看转换、仅服务端使用和推导的字段记录，不输出凭据值。
  审计字段不会进入生成的 Clash 配置。
- 生成器通过 `--trusted-nodes-file` 或 `CLASH_TRUSTED_NODES_FILE` 显式指定文件时，
  该文件必须存在；路径错误会在生成前停止，
  避免把缺失的 trusted 节点误当作空列表而覆盖现有输出。
- Loon 仅导出可完整表达的节点；不支持的协议或字段会跳过整条节点并列出原因，
  不会静默丢弃字段。请检查终端的导出数量与跳过清单；全部跳过时 Loon 文件为空。
  Loon 基础节点文件不生成代理链，因此也跳过 `allow-direct-exit: false` 的节点，
  避免导出后丢失禁止直出的限制。链出口应使用包含代理链和相应筛选规则的 Mihomo 配置。

生成器不会为缺失地区创建占位节点，地区策略组只会匹配实际生成的节点。

## 编辑和安全

`[Direct=false]`、`[ShowIP=true]` 等标记是生成器根据源配置自动写入的，不是 Clash
节点编辑界面的标准字段。应修改 `host_vars` 或 `trusted-nodes.yaml` 后重新生成，
不要只在客户端手动改节点名称。

不要提交 `vps-*`、`host.env`、私钥、Xray/Hysteria 配置、`nodes.yaml`、
`loon-nodes.conf` 或 `clash-vps.generated.yaml`。凭据一旦泄露，应立即轮换。

`home.yaml` 的策略组格式需要保持现有的对齐风格；编辑代理组时不要重写 `dns`、
`rules` 或 `rule-providers`。

两个脚本通过 `node_io.py` 统一读取 YAML：显式重复键会报错，合法的锚点和
`<<` 合并覆盖仍可使用。生成器的输出不能与已识别的输入文件重合，也会检查符号链接
及硬链接别名。替换前拒绝目录等非普通文件目标、非目录父路径，以及互为父子路径的输出。
所有请求的输出先在私有临时目录完成渲染、YAML 校验，再逐文件原子替换；
转换失败不覆盖旧输出，但多个文件最终写入时发生磁盘错误不具有跨文件事务保证。
服务端参数映射集中在 `node_conversion.py`；移动两个脚本时需同时带上这两个辅助模块。
服务端转换对已映射的字符串、字符串列表、布尔及非负整数参数检查类型，显式空值
不会被当成缺失值补默认；合法的空字符串、空列表、`false` 和 `0` 原样保留。
VLESS UUID 与密码一样保留首尾空格，不通过裁剪来修正输入；实际可用性仍需内核及连通性验证。
机场导入也检查 VMess UUID、Trojan 密码和 Shadowsocks 密码及 cipher；缺失、空值、
非法类型或换行会跳过整条节点并报告字段名，合法凭据的首尾空格保留。
普通输出与模板输出均保留嵌套扩展字段的浮点类型，包括科学计数法和非有限值；
保留 YAML 值不代表客户端支持该扩展字段或数值。
Loon 对 VLESS flow、REALITY 公钥/short-id 和 ALPN 增加类型检查，非法时跳过整条节点
并报告原因，不将数字等强制转换成字符串。

## DNS 与迁移条件

`home.yaml` 是面向 Mihomo / Clash Verge Rev 的空节点模板。迁移到新设备时，需加入
私有生成节点，并保留地区、角色和能力名称标记；普通订阅的原始节点名未必能命中这里的
节点池。首次启动还需要下载规则集和 GeoIP/GeoSite 数据，不能依赖旧设备已有的缓存。

- `respect-rules: true` 控制普通 DNS 上游连接的路由；DNS URL 中的 `#策略组`
  显式指定该查询的出口。节点域名由 `proxy-server-nameserver` 单独解析。
- `default-nameserver` 和 `proxy-server-nameserver` 当前都使用 `223.5.5.5`、
  `119.29.29.29`。跨境、受限网络或 IPv6-only 环境迁移时，需先验证这些 IPv4 DNS
  的可达性；独立解析链路不能保证解析服务器在所有网络都可用。
- `direct-nameserver` 当前使用阿里和 Google DoH，默认不继承全局 `respect-rules`；
  未设置的 `direct-nameserver-follow-policy` 默认为 `false`，DIRECT 出口域名解析
  不应被理解为始终沿用业务 `nameserver-policy`。参见
  [Mihomo DNS 文档](https://wiki.metacubex.one/config/dns/)和
  [内核 DNS 配置解析](https://github.com/MetaCubeX/mihomo/blob/Meta/config/config.go)。
- `redir-host` 下 `fake-ip-filter` 不负责选择 DNS 上游。DNS 监听端口 `1053`
  可以配合 TUN 劫持 53 端口工作；当前监听 `0.0.0.0`，迁移时应按本机或局域网用途
  检查监听范围。TUN 的实际权限、路由、系统 DNS 和客户端覆写需在目标设备检查。
- `ChinaDNS` 的首选项为 `REJECT`，是已有的分流选择，不代表独立的节点解析和
  DNS 引导查询也被禁止；不能由此宣称没有任何直连 DNS 查询。
- Stash 使用转换后的骨架，并单独加入私有节点；移除 DNS policy 和 URL 代理组后缀
  会改变解析行为，具体差异见前面的 Stash 说明。Loon 输出仅覆盖可表达的基础节点。
- 配置可以部署到 Windows 上的 Mihomo 客户端；节点管理脚本依赖 `fcntl`，需在
  Linux、macOS 或 WSL 中运行。迁移时应带齐共享 Python 模块并安装 PyYAML。

当前 `rule-providers` 仍直接引用上游 URL。独立快照仓库的自动同步不会自动修改这里的
规则来源；若要使用快照，需另行接入对应的地址或本地文件，并处理私有仓库的访问权限。

## 维护与验证

配置和脚本的行为以本 README 和测试为准。修改配置、节点生成或管理流程后运行：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile generate_raw_nodes.py manage_trusted_nodes.py node_io.py node_conversion.py generate_stash_config.py generate_stash_private.py
git diff --check
```

具备私有输入和 Mihomo 时，在自动清理的私有临时目录生成候选配置，使用已安装内核的
`-t` 校验；测试配置、缓存与生产目录隔离。单元测试验证代码行为，内核校验验证配置可加载；
两者均不能代替真实连通、DNS 分流、故障切换或 Stash 实机测试。
Mihomo 对 DNS policy 引用的 `classical` 规则集会提示只匹配其中的域名规则，
加载成功不代表这些规则集的 IP 条目也参与 DNS 匹配。
