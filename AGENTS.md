# 仓库协作约定

## 范围与安全

- 本仓库保存公开配置模板、生成脚本和测试。私有节点输入及生成配置包含凭据，
  不得提交、打印或写入公开文档；遵循 `.gitignore`。
- `generate_raw_nodes.py` 负责节点及代理链输出；`manage_trusted_nodes.py`
  负责私有 trusted inventory 管理。共享 YAML 读取在 `node_io.py`，
  服务端到客户端的参数映射在 `node_conversion.py`。
- `generate_raw_nodes.py` 输出含基础节点与可选代理链的 `clash-vps.generated.yaml`，
  还可输出仅含基础节点的 `nodes.yaml` 和 Loon 格式节点及选中代理链的 `loon-nodes.conf`。
  三者含凭据，均被 `.gitignore` 忽略；文件职责与更新命令见 README。
- `home.yaml` 是规则、筛选和 DNS 的基础；`stash-dns-policy.yaml` 独立保存 Stash
  专用 DNS policy。`generate_stash_config.py` 合并两者，生成公开的无节点骨架
  `home-stash.yaml`；`generate_stash_private.py` 再结合骨架、
  `clash-vps.generated.yaml` 和 `home.yaml`，生成唯一供 Stash 导入的
  `home-stash.private.yaml`。后者含凭据、被 Git 忽略。
- Clash Verge 使用 `home.yaml` 与节点生成器输出的 `proxies` 片段；本仓库脚本不会
  自动合并或重载生效配置。Loon 使用独立输出的 `loon-nodes.conf`。
- 保留用户已有改动。修改脚本不代表获准覆盖、重载 Clash Verge 生效配置，
  或替换 Stash 已导入的配置，也不代表获准提交或推送；这些操作须有用户明确授权。
- 修改 `home.yaml` 时保留原有对齐风格，避免无关的 DNS、规则和规则集重写。
- 清理工作区时只删除明确可重新生成的缓存或临时文件，例如 `__pycache__/`。
  不要把被 Git 忽略的 `clash-vps.generated.yaml`、`home-stash.private.yaml`、
  `nodes.yaml`、`loon-nodes.conf` 或 trusted inventory 当作垃圾删除；
  `home-stash.yaml` 虽然可重新生成，仍是私有 Stash 生成流程的公开中间文件。

## 参数与分流约束

- 已有 Clash 客户端参数必须完整保留，包含嵌套结构、未知扩展字段、`false`、
  `0`、空字符串及空结构；内部 `_` 开头的能力和审计元数据不属于导出参数。
- 缺失的可选参数不任意补默认值；必要参数缺失或非法时明确报错。
  从服务端配置转换时明确区分可映射、仅服务端和可推导字段，记录推导依据。
  不能可靠转换的字段要求完整客户端 inventory，不能通过忽略字段来“修复”。
- 连接地址与 SNI 分开处理；禁止导出服务端私钥。凭据首尾空格原样保留，
  不把非法类型转成字符串，不把换行悄悄替换成空格。
- Loon 不能表达某个客户端字段时跳过整条节点并报告原因，不能输出缺参数版本；
  已认证 SOCKS5 可导出，并保留 Loon 能表达的 TLS、SNI、证书校验和 UDP 参数。
  Loon `[Proxy Chain]` 只使用本次 Clash 生成时选中的链，入口须为 VLESS，
  且两端必须已导出到 `[Proxy]`；不满足条件的链跳过并报告原因。
  `[Direct=false]` 不影响 Loon 基础节点导出；Loon 的基础节点列表仍可能被
  用户手动选择直连，文档必须说明该限制。
  Loon 节点别名保留普通节点的 `地区.协议`，HomeIP 用 `地区.homeip.协议`，
  非 HomeIP、允许作链出口且禁止直出的 Exit 节点用 `地区.landing.协议`；代理链引用实际别名。
- 不生成缺失地区的占位或补位节点，也不通过复制节点、修改地区标签来填充空策略组。
- ShowIP 是出口节点的附加能力，不是独立出口角色。同地区普通 Core / Exit、ShowIP
  和 HomeIP 均可组链，但必须同时满足落地允许 chain-exit、入口允许 relay、链路协议
  匹配且物理节点不同；`allow_direct_exit` 不参与代理链资格判断。
- 修改名称、能力标记或组链规则时，必须同时测试 `home.yaml` 的实际筛选表达式。

## Stash 生成顺序

- 改动 `home.yaml` 或 `stash-dns-policy.yaml` 后，依次运行
  `python3 generate_stash_config.py` 和 `python3 generate_stash_private.py`；
  改动 `clash-vps.generated.yaml` 后只需重新生成 private。若改动的是 trusted
  inventory 或其他原始节点资料，先运行 `generate_raw_nodes.py` 更新节点输出。
- 不要手改 `home-stash.yaml`；私有生成器必须核对骨架与两个公开输入一致，
  再按 `home.yaml` 的筛选条件确定静态节点。空组固定为只含 `REJECT` 的
  `select` 组，不接收会使组成员自行变化的动态 `proxy-providers`。
  私有配置导出时展开重复 YAML 值，不输出自动生成的锚点引用。
  重新生成后仍需由用户在 Stash 中导入；本地文件变化不会更新已导入的配置。
- Stash 专用 DNS policy 及注释只维护在 `stash-dns-policy.yaml`，不得与
  `home.yaml` 的 DNS policy 重名。保留 `home.yaml` 作为基础；无法等价迁移的
  Stash 字段须在转换脚本和文档中说明，筛选契约变化须报错并审核。
- Hysteria2 将 Mihomo 的 `password` 映射为 Stash 的 `auth`，VLESS 将
  `servername` 映射为 `sni`；其余节点字段和值须保留。字段冲突、缺失认证
  或经策略组形成的代理链循环须报错。
- `proxy-server-nameserver` 在 Stash iOS/tvOS 3.6+ 才提供独立解析；
  3.4.1 的配置即使包含该字段，也不能据此认定代理服务器域名不会递归解析。
  规则集的 Mihomo `proxy` 字段在 Stash 转换时丢弃，远端资源下载错误应与
  代理节点连通错误分别核查。曾出现的 HY2/代理链超时后来恢复，根因未确认；
  不要把一次测速或 UDP 错误直接归因为生成脚本缺字段。

## 文件写入

- 两个脚本统一使用严格 YAML 加载器，拒绝显式重复键；支持合法锚点和
  `<<` 继承覆盖。错误信息不能泄露原始凭据行。
- 检查所有输出与输入的路径、符号链接和硬链接冲突。
- 先完成所有请求格式的渲染和校验，再逐文件原子替换。私有临时目录和
  `0600` 文件权限必须保留；不要声称跨多个输出文件具有磁盘故障事务保证。
- trusted inventory 的权限校验、锁、备份及原子写入不得被回归破坏。

## 验证与交付

修改脚本后运行：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile generate_raw_nodes.py manage_trusted_nodes.py node_io.py node_conversion.py generate_stash_config.py generate_stash_private.py
git diff --check
```

- 参数映射的改动需要覆盖缺失值、显式空值、非法类型和嵌套字段；涉及输出安全的
  改动需要覆盖失败时旧文件不变、重复键和路径别名。
- 能访问私有输入和客户端内核时，在自动清理的私有临时目录生成候选配置，
  使用已安装的 Mihomo `-t` 校验；缓存及测试配置与生产目录隔离。
- 内核配置校验通过不等于实际连通、DNS 分流或灾备切换已通过测试。
  交付时区分单元测试、配置加载校验和真实网络测试，说明仍存在的警告及限制。
- 提交前检查暂存清单和差异，确保没有凭据及生成产物；推送后确认远端分支与
  本地提交一致。行为变化同步维护 `README.md` 和相应测试。
