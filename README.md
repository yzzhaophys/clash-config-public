# Clash Verge Rev home configuration

可复用的 Mihomo/Clash Verge Rev 配置模板，以及用于生成自建节点和代理链的脚本。

## 文件

- `home.yaml`：主配置模板。它负责策略组和分流规则，`proxies` 列表保持为空。
- `home-stash.yaml`：由 `home.yaml` 生成的 Stash 配置骨架；不包含节点、订阅或代理链。
- `generate_raw_nodes.py`：读取私有节点配置，生成基础节点和可选的代理链。
- `generate_stash_config.py`：将公开的 `home.yaml` 转换为 Stash 配置骨架。

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
- `--home-template PATH`：指定模板模式校验所用的 `home.yaml`；生成器会校验组引用和实际节点/代理链筛选。
- `--raw-output PATH`：额外输出仅含基础节点的 YAML。
- `--loon-output PATH` / `--no-loon`：指定 Loon 输出文件，或关闭 Loon 输出。

生成 Stash 配置骨架：

```bash
python3 generate_stash_config.py
```

该文件只保留 Stash 的策略组、规则集和分流结构；节点、代理提供者和生成的代理链
必须在私有环境中另行加入。原本使用 `empty-fallback: REJECT` 的自动组会转换为显式
的 `REJECT` 候选，且筛选表达式显式保留该候选，防止它被过滤成空组；实际空组行为仍须在 Stash 上验证。
`DirectExit` 组把原先的 `filter` + `exclude-filter` 合并成 Stash 文档支持的
单个 `filter`：通过有限状态机将排除词转换为不含前瞻的普通正则，保留描述中的
排除词匹配，同时允许源配置接受的未知标签。生成表达式较长，不建议手动修改；
转换前还会检查代理组引用、循环和规则目标；`DirectExit` 的转换根据筛选契约识别
Download 组，因此调整组名前缀不会让 Stash 输出静默失效。
源筛选发生变化时脚本会报错，要求重新审核转换。Stash 延迟测试地址和超时应在将来加入的
节点上配置 `benchmark-url`、`benchmark-timeout`。`select` 组设置 `interval: -1`
关闭默认的递归周期测速，自动组保留原模板的 30/45/90 秒间隔和 `lazy: true` 设置。
源配置中 115 个手动 `PASS` 选项未出现在 Stash 文档的内置出口列表中，
转换时将其移除；`DIRECT`、`REJECT`、`REJECT-DROP` 保留。
DNS 中仅保留 Stash 可表达的精确域名、通配域名和 `geosite:` policy，并保留
`proxy-server-nameserver` 用于独立解析代理节点域名（要求 Stash iOS/tvOS 3.6+、
macOS 4.3+，旧版本不能依赖该字段防止解析递归）；模板里的
Clash Meta `rule-set:` DNS policy 和 DNS URL 代理组后缀会被移除，普通 `RULE-SET`
分流规则不受影响，但这不代表原 DNS 策略完全等价：被移除的 DNS policy 不再单独选 DNS，
DNS 请求也不再使用 URL 后缀指定的代理组，而是依赖 `follow-rule` 和普通路由规则。
Stash 的通配 DNS policy 优先于 geosite，因此 `+.*` 被迁移到默认 `nameserver`，
避免遮蔽 geosite 策略，并保留其 DNS 服务器顺序。参见
[Stash DNS 文档](https://stash.wiki/en/features/dns-server)。
输出通过同目录临时文件校验后原子替换，权限保留 `0600`；替换失败时旧文件不变。
当前只转换 HTTP rule-provider 和 `empty-fallback: REJECT`，遇到其他值明确报错。
自动化测试覆盖源配置对比、筛选反例和写入失败；Mihomo 辅助加载检查不等于
Stash 实际运行。接入私有节点后仍需在目标 Stash 版本上测试导入、DNS、空组和故障切换。

Stash 对比审查中需要注意的行为差异：

- DNS policy 支持多个服务器，但采用并发查询，列表顺序不是主备优先级。
  `follow-rule` 路由的是 DNS 服务器连接，不保证 DNS 与被查询网站使用相同出口。
- `📡.<DNS>--ChinaDNS` 在两份模板中保持原模板的 `REJECT` 首选，拒绝命中该组的
  DNS 连接，并保留手动选择 `DIRECT` 的选项。这是既定拦截策略，不能仅因配置了
  国内 DoH 就判定有误；如遇解析异常，应先检查实际连接日志和规则命中。
  已保存的客户端选择不会因候选顺序变化自动重置。独立节点 DNS 不跟随该组。
- 35 个动态节点池中的 `REJECT` 是常驻候选，不是仅在空组时注入。
  静态校验只证明候选存在及筛选保留；健康节点存在、全部失败、节点恢复时，
  `url-test` 和上层 `fallback` 如何处理它，必须在 Stash 上验证。
- 组级 `tolerance`、`max-failed-times` 和测速参数未等价迁移；Stash 使用节点级
  测速结果，故障判定及切换时机可能与 Mihomo 不同。保留 30/45/90 秒间隔和
  `lazy`，暂未针对移动端延长间隔，以免改变现有恢复速度。
- 150 个规则集保留原 URL、缓存路径和更新间隔，但移除了下载用的 `proxy`。
  这不构成上游 URL 失效时自动切换到备份仓库；Stash 内首次下载和更新仍须验证。

参考：[策略组](https://stash.wiki/proxy-protocols/proxy-groups)、
[延迟测试](https://stash.wiki/proxy-protocols/proxy-benchmark)、
[规则集合](https://stash.wiki/rules/rule-set)。

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
- `allow_showip: true`：节点名增加 `[ShowIP=true]`，进入对应的 ShowIP 节点组和代理链。
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
HomeIP；`[ShowIP=true]` 只让对应基础节点或代理链进入 ShowIP 策略组。

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
以下命令从本仓库根目录执行：

```bash
chmod 600 /path/to/nat-node.yaml
python3 manage_trusted_nodes.py merge \
  --target ~/.config/clash/airport/trusted-nodes.yaml \
  --source /path/to/nat-node.yaml
python3 manage_trusted_nodes.py merge \
  --target ~/.config/clash/airport/trusted-nodes.yaml \
  --source /path/to/nat-node.yaml \
  --apply
```

NAT 主机退役时按稳定 `id` 先预览、再删除；省略 `--protocol` 会删除该物理节点登记的全部协议，
指定 `--protocol vless|hysteria2|socks5` 时只删除一个协议：

```bash
python3 manage_trusted_nodes.py remove \
  --target ~/.config/clash/airport/trusted-nodes.yaml \
  --id provider-us-01
python3 manage_trusted_nodes.py remove \
  --target ~/.config/clash/airport/trusted-nodes.yaml \
  --id provider-us-01 \
  --apply
```

合并或删除只修改私密事实源。应用后必须使用当前已审查的选项重新运行生成器并重新加载客户端；
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
- `ShowIP` 组筛选 `[ShowIP=true]`；
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
| Max.Traffic | `fallback` | 优先引用 Download，全部失效时回退 `♾️.Line-[Final]` |
| Download | `url-test` | 每 30 秒检测获准下载的真实节点 |
| 其他 Chain / DirectExit（CN 除外） | `url-test` | 每 30 秒检测各自节点池 |
| 普通国家 Line（CN 除外） | `fallback` | 检测间隔 45 秒，直出优先、同地区代理链备用 |
| 内层 HomeIP / ShowIP Line | `fallback` | 检测间隔 45 秒，代理链优先、同地区同用途直出备用 |
| 跨地区、Final、Low.Latency | `fallback` | 每 90 秒检测候选线路 |
| CDN 业务入口 | `select` | 默认引用 Max.Traffic，也可手选 Low.Latency；不增加跨组自动灾备层 |
| Americas / Oceania / Europe | `fallback` | 每 90 秒检测；本地区线路优先，`♾️.Line-[Final]` 作为跨地区备用 |
| CN Line / CN DirectExit | 单子项 `select` | 当前最终指向 DIRECT，不提供回国代理节点或自动灾备 |

业务组引用的 `🌏🇺🇸.Line-[US.HomeIP]`、`🌏🇺🇸.Line-[US.ShowIP]` 等外层 Line
每 90 秒检查一次，先使用对应的内层用途线路；内层线路没有可用候选时再回退到同地区普通
Line。内层 HomeIP / ShowIP 线路仍只在各自的 Chain 和 DirectExit 组之间切换。

所有自动组设置 `lazy: true`、`max-failed-times: 2`；`url-test` 节点池使用
`timeout: 3000`，普通国家线路 `fallback` 使用 `timeout: 4000`，HomeIP / ShowIP、跨地区及入口线路
使用 `timeout: 5000`。
`url-test` 使用 `tolerance: 50` 毫秒，减少健康节点间的小幅延迟切换；
该容差不会阻止内核替换已被探测判定失效的节点。失败阈值只用于触发额外检查，
其计数受内核版本、失败类型及时间窗口影响，不保证两次业务请求失败就换线。
检测周期也不是故障恢复时限：探测耗时、多层状态更新、实际流量与探测流量的差异
都会影响恢复。底层检测更频繁，会增加后台探测开销。

`☁️.<Global>--CDN` 的首选项为 Max.Traffic，保留 Low.Latency、DIRECT 等手动选项。
配置启用了 `store-selected`，已有选择可能优先于列表首项。CDN 不会在 Download
整体失效时自动改选 Low.Latency 或 DIRECT，需要手动选择；Download 内部仍自主换节点。
下载业务仍走 Max.Traffic；它优先使用 Download 节点池，Download 全部故障时明确
回退到 `♾️.Line-[Final]`，因此该灾备路径可能使用未带 `allow_download` 标记的普通节点。
`empty-fallback: REJECT` 只处理节点池为空，不等于全部节点测速失败时的跨组灾备。
缺失地区不会生成占位节点。地区 `fallback` 只有在
备用节点池实际包含可用节点时才具有备用路径；两个池均为空的地区线路不能使用。

当前统一探测 Apple 测试页面并要求 HTTP 200，它只能代表该地址可达，不能证明
linux.do、其他站点或 UDP 正常，也不衡量下载带宽。上层探测一个子组时，检验的
是子组当时选中的路径，不能替代底层节点池的独立检测；不能承诺嵌套后瞬时恢复。
底层保留独立检测配置，实际调度受 `lazy: true` 影响。单子项 `select` 入口不再重复定时探测。Americas / Oceania /
Europe 的 `fallback` 会按列表顺序使用本地区线路，全部本地区候选失效后才尝试
`♾️.Line-[Final]`；它关注可用性，不按延迟重新排序。

加载候选配置后应分别验证：

1. 查看 Download 的实际成员，确认四个 JP 节点符合名称筛选及下载能力限制；
   自动组保持自动选择，检查面板/API 是否存在手动固定的 `fixed` 状态。
2. 在隔离测试环境使当前节点失效，观察组内检测历史和 `now` 是否变化，
   再用新连接访问目标站点；旧的 TCP/下载连接不能自动迁移，应用需要重连。
3. 测试 Download 全部失效时，Max.Traffic 是否按 Download → `♾️.Line-[Final]` 回退，
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
- 显式指定 `--trusted-nodes-file` 时，该文件必须存在；路径错误会在生成前停止，
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
及硬链接别名。所有请求的输出先在私有临时目录完成渲染、YAML 校验，再逐文件原子替换；
转换失败不覆盖旧输出，但多个文件最终写入时发生磁盘错误不具有跨文件事务保证。
服务端参数映射集中在 `node_conversion.py`；移动两个脚本时需同时带上这两个辅助模块。
服务端转换对已映射的字符串、字符串列表、布尔及非负整数参数检查类型，显式空值
不会被当成缺失值补默认；合法的空字符串、空列表、`false` 和 `0` 原样保留。
VLESS UUID 与密码一样保留首尾空格，不通过裁剪来修正输入；实际可用性仍需内核及连通性验证。
Loon 对 VLESS flow、REALITY 公钥/short-id 和 ALPN 增加类型检查，非法时跳过整条节点
并报告原因，不将数字等强制转换成字符串。
通过命令行或 `CLASH_TRUSTED_NODES_FILE` 显式指定的 inventory 不存在时均会报错，
不会静默退回其他事实源。

## 维护与验证

生成器的规则以本 README 和测试为准。修改命名、能力开关、代理链或输出格式后运行：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile generate_raw_nodes.py manage_trusted_nodes.py node_io.py node_conversion.py generate_stash_config.py
git diff --check
```
