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
- `Loon-home.template.lcf` 是不内嵌 CA 的私有完整配置模板；非交互模式显式指定
  `--loon-full-output`，或交互模式选择生成时才写入完整 Loon 配置。六个生成标记分别承载
  Proxy、Proxy Chain、Route、Line、Chain、DirectExit；固定业务组和规则不得由节点
  生成器重写。每台设备需在 Loon 中自行生成 CA，并在系统中安装和信任；
  模板和完整输出均须保持私有且不得提交。
- `home.yaml` 是 Mihomo/Clash Verge 的规则、筛选和 DNS 基础；Loon 使用独立
  输出的 `loon-nodes.conf`，其规则文件另行维护。Loon 的 `[Proxy Group]` 沿用
  `home.yaml` 的 Route、Line、Chain、DirectExit 组名和顺序，仅引用实际导出的
  节点、代理链与非空下级组；本地 `[Proxy]` 节点由生成器筛选，不用 `[Remote Filter]`。
- Clash Verge 使用 `home.yaml` 与节点生成器输出的 `proxies` 片段；本仓库脚本不会
  自动合并或重载生效配置。
- 保留用户已有改动。修改脚本不代表获准覆盖、重载 Clash Verge 生效配置，
  也不代表获准提交或推送；这些操作须有用户明确授权。
- 修改 `home.yaml` 时保留原有对齐风格，避免无关的 DNS、规则和规则集重写。
- 清理工作区时只删除明确可重新生成的缓存或临时文件，例如 `__pycache__/`。
  不要把被 Git 忽略的 `clash-vps.generated.yaml`、`nodes.yaml`、
  `loon-nodes.conf`、私有 Loon 模板、完整配置、设备导出或 trusted inventory 当作垃圾删除。
  `*:Zone.Identifier` 是 Windows 下载标记，可清理；设备导出配置可能包含 CA，不能据此当作缓存删除。

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
python3 -m py_compile generate_raw_nodes.py manage_trusted_nodes.py node_io.py node_conversion.py
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

## 节点输入范围

- `generate_raw_nodes.py` 只读取自建 VPS 和 trusted inventory；不读取机场订阅、
  不提供订阅节点选择或订阅 DNS 策略导入。交互流程用于选择现有节点、代理链和可选完整 Loon 输出。
- 两个管理/生成脚本共享 `default_trusted_nodes_file()`；默认 inventory 为
  `~/.config/clash/trusted-nodes.yaml`，不再搜索历史 airport 目录。
  新位置使用 `CLASH_TRUSTED_NODES_FILE`、生成器 `--trusted-nodes-file` 或管理器 `--target`，
  不恢复 `CLASH_AIRPORT_DIR` / `--airport-dir`。历史私有输入文件不得作为清理对象删除。
- Loon 完整配置的格式以设备导出为参考；私有模板使用 `v4-only`、紧凑业务组逗号、
  单逗号远端规则。保留节点的 ALPN 和 UDP 参数，不依据 App 导出时的省略删除参数。
  设备导出测试配置含 CA 和凭据，必须忽略提交且保持 `0600`；模板不得包含设备 CA。

- trusted inventory 用稳定 `id + proxy.type` 更新时保留位置；删除后重导入追加到末尾，
  可能改变生成编号。Route / Line 等组及代理链引用在生成时同步更新，不依赖固定编号；
  不为维持编号复制节点或增加占位节点。
- Loon 固定内容以私有模板为维护入口；节点及链以自建输入和 trusted inventory 为入口。
  修改上游后重新生成并由用户导入 App；生成产物的手工编辑不会回写上游。

- 完整 Loon 配置写入前检查跨节点/链/组的名称冲突、组引用及循环；等号空格变化
  不应绕过检查。远端 `policy` 与本地 `FINAL` 引用校验不代表完整 Loon 语法验证。
