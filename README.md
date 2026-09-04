# learning-wiki（lw）

学习型 LLM Wiki 个人知识库系统核心。单用户、本地优先、macOS + Obsidian。

核心闭环：外部资料 → **不可变来源版本** → 可搜索、可引用 → 因真实使用而沉淀 →
闭卷回忆与反馈 → 延迟复习 → 项目应用 → 新证据推动更新。

- **事实源**：Markdown / YAML / JSON / JSONL（Obsidian Vault 内，可由 Git 管理）
- **派生状态**：SQLite + FTS5（`.learning-wiki/`，可随时删除，`lw rebuild` 重建）
- **Agent 层**：[Claudian](https://github.com/YishenTu/claudian)（Obsidian 内嵌
  Claude Code），经 vault 根目录 `AGENTS.md`/`CLAUDE.md` 规则 + MCP server（M2）+
  Skills 集成

## 当前状态

- [x] **M0 数据底座**：契约模型（pydantic → JSON Schema 导出）、路径防护
  （越界/符号链接逃逸）、SQLite migration、SafeFileWriter（原子写 + fsync +
  崩溃注入钩子）、init-vault、rebuild / lint / doctor
- [x] **M1 捕获、来源与搜索**：三类输入（粘贴文字 / Markdown/TXT / 公开网页）、
  不可变版本、Evidence 证据锚点（Obsidian Block ID + span_hash）、SSRF 防护
  （connect 时刻 IP 校验 + SNI 固定）、中文 FTS（jieba + unicode61）、
  重复检测与版本复用、删库重建
- [x] **M2 按需 Wiki 沉淀、提案与安全写入**：ChangeProposal（五项确定性校验）、
  staging 前后像 + FilePlan 状态机（prepare → execute → finalize）、崩溃恢复
  （幂等续跑 / 回 pending / needs_manual + 告警）、严格 unified diff 应用器、
  GitAdapter（pathspec 限定提交、用户 staged 内容时拒绝）、Claim Block 解析
  与反向引用索引（证据 → 受影响 Claim）、MCP server 9 工具（写边界：只能
  创建待审批提案）、`lw proposal list/show/diff/validate/apply/reject`
  （apply 强制 TTY 确认，非 TTY 拒绝、无 --yes 旁路）
- [x] **M3 学习与复习（学习机制升级）**：能力目标（Goal/Capability/CompletionPolicy，
  8 级能力层级、起点诊断、>5 能力拆分建议）、误解模型（10 种错误类型、候选/确认/
  复发/解决状态机、高置信度错误优先、失败干预不重复）、迁移与真实应用
  （近迁移/远迁移/real_application 证据分离、ApplicationChallenge、先预测后结果、
  用户确认）、Learning Orchestrator（§6.2 固定顺序确定性路由、会话预算与结束原因、
  先作答后反馈）、知识快照与再验证（needs_review/needs_revalidation、历史不改写）、
  知识更新候选（不直接改 Wiki）、Views 可重建；SQLite 派生表（migration 002/003）、
  删库重建；CLI（goal/diagnostic/learn/misconception/challenge/application/learning）
  与 MCP 学习类 14+2 工具（护栏：不能伪造作答、不能自动确认应用、不能直标 achieved）
- [ ] M4 更新、完整性与发布

## 安装

```bash
make install        # uv sync
make test           # 82 个测试（单元 / 集成 / 对抗 / 故障注入）
make lint           # ruff + mypy (strict: domain/application)
make check-schemas  # Schema 漂移检查（CI 门禁）
```

## Agent 接入（Claudian / Claude Code）

在 Vault 的 MCP 配置（如 `.claude/mcp.json`）中注册：

```json
{
  "mcpServers": {
    "learning-wiki": {
      "command": "<repo>/.venv/bin/lw",
      "args": ["mcp", "--vault", "/path/to/Vault"]
    }
  }
}
```

工具边界（规格 8.2）：读工具（status/search/read_source_version/read_evidence/
get_wiki_context/list_update_candidates/lint）+ 写工具（submit_proposal/
validate_proposal——只能创建待审批提案）。**没有**直接应用提案、删除来源或
覆盖 Thoughts 的工具：`lw proposal apply` 必须由用户在 TTY 下执行。
学习类工具（M3，学习机制升级思路 §8.1）：create/get_goal、start_diagnostic、
get_next_activity、submit_attempt/confidence/feedback、propose/update_misconception、
create_application_challenge、record_application_prediction/outcome、
get_capability_evidence、list_revalidation_tasks（+ activate_goal、
resolve_misconception）。护栏：Agent 不能伪造用户回答（attempt 必须来自真实会话
且带非空回答）、不能自动确认真实应用成功（outcome 必须 user 确认）、不能直接把
Goal 标记 achieved——achieved 由确定性规则从事件证据派生；学习结果只能创建
知识更新候选，不能直接修改 Wiki。

## 学习闭环（M3）

```bash
lw goal create --title "…" --capability 'cap_x|能解释…|explain' --note-id <wiki笔记> --activate
lw goal show GOAL_ID            # 能力证据与缺口（无 mastery 百分比）
lw diagnostic start GOAL_ID    # 起点诊断（已有充分证据的活动会被跳过）
lw learn next GOAL_ID           # 确定性下一活动（reason 说明为何推荐）
lw learn object-create --session S --goal G --title … --prompt … --type explain --capability cap_x
lw learn attempt S OBJ --response "…" --confidence 80   # 闭卷作答（先于反馈）
lw learn feedback S OBJ --result successful|partial|failed
lw misconception list/show/propose/resolve/intervene
lw challenge create GOAL_ID --capability cap_x --problem "…"
lw application predict CH --context … --knowledge … --reasoning … --prediction …
lw application record CH --action … --outcome … --assessment successful   # TTY 用户确认
lw learning revalidate          # 知识快照过期检测 → needs_review / needs_revalidation
lw learning views               # 重建 40 Learning/Views/（非事实源）
```

事件（`40 Learning/Events/YYYY-MM/{session}.jsonl`）append-only；`achieved` /
能力层级 / Views / SQLite 全部为派生状态，`lw rebuild` 可从 YAML + JSONL 重建。

## 快速开始（垂直切片 V0）

```bash
# 1. 初始化 Vault（幂等，不动既有笔记；也可对已有 Vault 执行）
uv run lw init-vault ~/TestVault

# 2. 捕获：粘贴文字 / 导入文件 / 公开网页
uv run lw inbox add --text "一段中文文字…" --vault ~/TestVault
uv run lw inbox add --file ./note.md --vault ~/TestVault
uv run lw inbox add --url https://example.com --vault ~/TestVault
uv run lw inbox list --vault ~/TestVault

# 3. 处理 → 不可变来源版本 + 证据块 + 全文索引
uv run lw inbox process <ITEM_ID> --vault ~/TestVault

# 4. 搜索（结果含 Evidence ID、匹配原因、来源版本、时间）
uv run lw search "关键词" --trace --vault ~/TestVault

# 5. 打开证据：原文 + span_hash 校验 + 可点击的 Obsidian 块锚点
uv run lw evidence show <EVIDENCE_ID> --vault ~/TestVault

# 6. 完整性与健康
uv run lw lint --vault ~/TestVault
uv run lw doctor --vault ~/TestVault

# 7. 删除派生库后重建，检索结果不变（事实源优先的验证）
rm -rf ~/TestVault/.learning-wiki
uv run lw rebuild --vault ~/TestVault
uv run lw search "关键词" --vault ~/TestVault
```

未指定 `--vault` 时使用 `LW_VAULT` 环境变量或当前目录。

## Vault 结构（事实源）

```text
My Vault/
├── 00 Inbox/                      # 用户暂存
├── 10 Sources/{source_id}/        # 不可变来源版本
│   ├── manifest.yaml               #   元数据（ruamel round-trip，保留未知字段）
│   └── versions/v0001/
│       ├── original.*              #   原件（HTML 快照 / 原始文本 / 原文件）
│       ├── content.md              #   规范化正文（含 ^ev-… 证据块锚点）
│       └── evidence.jsonl          #   EvidenceRef（绑定 4 元组哈希）
├── 20 Thoughts/                    # 用户所有
├── 30 Wiki/                        # 沉淀页面（M2 起经提案修改）
├── 40 Learning/                    # 学习对象与事件（M3）
├── 50 Projects/
├── 90 Archive/
├── _System/Learning Wiki/
│   ├── Config.yaml                 # 目录映射等全部配置
│   ├── Operations/*.jsonl          # 审计日志
│   └── Proposals/                  # M2
├── AGENTS.md / CLAUDE.md           # Agent 十一条规则（init-vault 写入）
└── .learning-wiki/                 # 设备级派生状态（永远排除出 Git）
    ├── knowledge.db                #   SQLite + FTS5，可删可重建
    ├── inbox.jsonl                 #   瞬时 Inbox（不参与重建）
    ├── locks/                      #   进程写锁（flock）
    └── staging/                    #   M2 安全写入
```

## 关键设计

- **来源不变量**：版本目录写入顺序 original → content.md → evidence.jsonl →
  manifest.yaml（manifest 是提交点，rebuild 只认 manifest）。纠正与重提取一律
  创建新版本（`v0002`），旧版本永不修改。
- **证据不变量**：每条证据绑定 `source_id + version_id + content_hash +
  span_hash`；块 ID（`^ev-…`）使用完整 ULID（截断会碰撞——实测同一毫秒内
  连续捕获前 6 位甚至前 14 位都相同，单调 ULID 只在随机部分末尾递增）。
- **重复检测**：web 按 canonical_url 定位来源，同内容哈希复用版本；相同内容
  不同上下文不合并（各自保留来源身份）。
- **SSRF 防护**：解析后校验全部 DNS 记录 → 重写 URL 主机为已验证 IP 直连 +
  `sni_hostname` 固定 TLS/证书主机名（connect 时刻保证，防 DNS rebinding），
  重定向逐跳重校验；拒绝环回 / 私网 / 链路本地（含云元数据）/ ULA / 组播 /
  保留段 / 非 http(s)。
- **受限网页降级**：登录墙 / 反爬 / 正文过短 → 失败原因写入 Inbox，引导改用
  粘贴文字或文件导入。
- **内容哈希与锚点**：`content_hash` 绑定提取内容（不含系统追加的锚点行），
  lint 比对时先剥离 `^ev-…` 行——篡改必被发现，与时间戳无关。

## 测试与验收

```text
tests/unit/         哈希规范化（表驱动）、ID 规则、路径逃逸、块锚点稳定性、
                    migration 幂等、崩溃注入、diff 应用器（200 次随机往返）
tests/integration/  三类输入捕获、重复复用、同内容不同源、登录墙降级、
                    prompt-injection 内容是数据、删库重建等价、V0 CLI 端到端、
                    提案校验/应用/冲突/归档、崩溃注入恢复（子进程 kill 后重启）、
                    Git pathspec 边界、MCP 工具与写边界
tests/adversarial/  SSRF 全用例（含 redirect 跳私网、IPv4-mapped、云元数据）、
                    篡改检测（content/original/span_hash）、未来 schema 只读、
                    非 TTY apply 拒绝、staged 时 Git 拒绝提交
```

## 设计文档

需求与验收规格见 `../AI-Knowledge/个人知识库系统实现方案.md`；技术方案见
`~/.claude/plans/immutable-puzzling-sketch.md`。

## 目录组织（多领域 Vault）

工具一处安装，按领域各建一个 Vault（检索、学习闭环、Git 天然隔离）：

```text
~/Documents/knowledge/
├── learning-wiki/          ← 本工具（不用 Obsidian 打开）
├── AI-Knowledge/           ← AI 领域 Vault（Obsidian 打开这个）
└── Investing/              ← 其他领域 Vault（Obsidian 新建后 lw init-vault）
```

日常用法（建议在 ~/.zshrc 里设别名）：

```bash
alias lwai='uv run --project ~/Documents/knowledge/learning-wiki lw --vault ~/Documents/knowledge/AI-Knowledge'
lwai inbox add --url https://…
lwai search "关键词"
```

## 后续里程碑

- **M3**：LearningObject、闭卷作答（先作答后反馈的确定性保证：attempt 事件
  必须先于 feedback 事件落盘）、ReviewScheduler（规格 6.6 表格，纯函数）、
  误解候选（高置信度错误）、MCP 学习类 5 工具
- **M4**：反向影响分析（证据 → Claim → 页面 → 学习对象 → 待核验任务）、
  外部修改检测、Schema 迁移、发布文档
