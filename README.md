# OA Daily Digest

一个零依赖的每日开放获取（Open Access）文献雷达，聚焦三个方向：

- **MKIDs 探测器**：微波动能电感探测器、超导谐振器阵列、读电子学、毫米/亚毫米焦平面
- **亚毫米波天文**：亚毫米波/远红外/太赫兹观测、仪器、连续谱相机、星际介质
- **分子云磁场**：分子云与致密核磁场、尘埃偏振、Zeeman 测量、磁流体力学

它每天从 **arXiv Atom API / arXiv RSS** 和 **OpenAlex** 抓取新记录，合并去重、按关键词相关性打分，并为每篇论文生成一段**自动提取的速读总结**。生成：

- `docs/index.html`：可搜索、筛选、收藏、导出 BibTeX 的静态仪表盘
- `docs/digests/YYYY-MM-DD.md`：当天的 Markdown 日报
- `docs/digests/index.md`：日报归档目录
- `docs/data/papers.json`：结构化数据，方便二次开发
- `data/archive.json`：滚动归档（默认保留 60 天）

页面只保存论文的**标题、作者、摘要、自动速读总结和公开链接**，不下载、不托管出版商全文；你点链接即可去 arXiv 或期刊页面阅读。

> 这个文件夹里已经附带一份抓取于 **2026-10-20** 的归档（约 90 篇），上传到 GitHub 后即可直接看到成品页面；之后由 Actions 每日增量更新。

页面不依赖任何前端框架或 CDN，断网时也能直接打开本地 HTML。

---

## 快速开始（本地）

要求 Python 3.10+，不需要安装第三方包。

```powershell
python scripts/fetch_papers.py --days 7
start docs\index.html
```

第一次建立归档时，建议用 `--seed` 回填一段时间的文献，避免把 60 天的旧论文全部标成“今日新增”：

```powershell
python scripts/fetch_papers.py --days 60 --seed
start docs\index.html
```

之后每天正常运行 `--days 7` 即可；`first_seen` 会记录真正首次入库的日期。

只重新生成页面、不联网：

```powershell
python scripts/fetch_papers.py --offline
```

常用参数：

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--days` | `7` | 抓取最近 N 天发表的记录（日报按“首次收录”统计） |
| `--archive-days` | `60` | 滚动归档保留天数 |
| `--max-per-query` | `200` | 每个查询最多抓取多少条 |
| `--delay` | `3` | 请求间隔秒数，建议保留以遵守 API 礼仪 |
| `--offline` | 关闭 | 跳过联网，仅从 `data/archive.json` 重建页面 |
| `--seed` | 关闭 | 回填模式：把旧论文的 `first_seen` 设为发表日期，只有今天真正新出现的论文才算“今日新增” |
| `--skip-arxiv-api` | 关闭 | 跳过 arXiv API，但仍读取每日 arXiv RSS（调试或 API 限流时使用） |
| `--config` | `config/topics.json` | 主题配置 |
| `--out` | `docs` | 输出目录 |

首次运行会把抓取窗口内的记录全部记为“今日新增”；使用 `--seed` 后则只保留当天的新记录。

---

## 部署到 GitHub Pages（每日云端更新）

1. 新建一个 GitHub 仓库，把本目录推送到默认分支（例如 `main`）。
2. 打开仓库 **Settings → Pages**，把 **Source** 设为 **GitHub Actions**。
   - 工作流里也带了 `actions/configure-pages@v5` 的自动启用参数；如果组织策略不允许，手动设置即可。
3. 打开 **Actions → Daily OA digest → Run workflow** 先手动跑一次，确认页面生成。
4. 之后工作流会按 cron 自动运行：

   ```text
   17 22 * * *   # UTC，对应北京时间次日 06:17
   ```

   想换时间，修改 [.github/workflows/daily.yml](.github/workflows/daily.yml) 里的 cron 即可（cron 使用 UTC）。
5. 页面地址：`https://<用户名>.github.io/<仓库名>/`。

### 建议设置一个 OpenAlex 联系邮箱

OpenAlex 提供“polite pool”，带上联系邮箱可以获得更稳定的配额：

- 仓库 **Settings → Secrets and variables → Actions → Variables**
- 新建变量 `OPENALEX_MAILTO`，值为你的邮箱

不设置也能运行，但高峰期可能更容易遇到 HTTP 429；脚本会自动退避重试。

### 上传提示

用 GitHub 网页的 **Add file → Upload files** 拖拽上传时，请确认 `.github/workflows/daily.yml` 也被上传（有些浏览器拖拽文件夹时会忽略隐藏目录）。如果发现工作流文件没上去，可以：

- 用 GitHub Desktop / `git push` 推送整个目录；或
- 在 GitHub 网页手动新建 `.github/workflows/daily.yml`，把本目录里的同名文件内容粘贴进去。

`.gitignore` 只是可选的忽略规则，不影响网页运行。

### 工作流做了什么

1. `actions/checkout` 拉取仓库。
2. `setup-python` 安装 Python 3.12。
3. 运行 `scripts/fetch_papers.py`，抓取、合并、生成 `data/archive.json` 与 `docs/`。
4. 把生成结果提交回仓库（保留历史归档）。
5. 上传 `docs/` 并部署到 GitHub Pages。

如果仓库开启了分支保护、不允许 Actions 直接 push，需要给 `github-actions[bot]` 放行，或者去掉“Commit generated archive and pages”步骤，仅部署本次构建。

---

## 配置主题与关键词

所有主题都在 [config/topics.json](config/topics.json) 中。每个主题包含：

```json
{
  "id": "mkid",
  "label": "MKIDs 探测器",
  "color": "#8b5cf6",
  "icon": "◈",
  "description": "一句话描述",
  "arxiv_categories": ["astro-ph.IM", "physics.ins-det"],
  "arxiv_queries": [
    "all:\"kinetic inductance\"",
    "all:MKID",
    "all:LEKID"
  ],
  "openalex_queries": ["kinetic inductance", "MKID", "LEKID"],
  "require_groups": [
    ["microwave kinetic inductance", "kinetic inductance", "mkid", "mkids", "lebid"]
  ],
  "min_score": 7,
  "weights": {
    "microwave kinetic inductance detector": 12,
    "kinetic inductance": 5,
    "mkid": 8
  }
}
```

关键字段：

- `arxiv_queries`：一组**简单** arXiv 查询。脚本会为每条查询追加 `submittedDate` 日期窗口，再在本地用主题关键词和分类过滤；拆成小查询可以避免超大布尔查询被 arXiv 限流。
- `arxiv_categories`：该主题允许的 arXiv 分类；RSS 或 API 返回的论文至少命中一个分类才会归入该主题。
- `openalex_queries`：OpenAlex 的 `search` 查询列表；每条结果都会再用本地关键词规则过滤。
- `require_groups`：必须同时命中的关键词组。比如“分子云磁场”要求同时出现“分子云/致密核”和“磁场/偏振/Zeeman”。
- `weights`：关键词权重。标题命中按 2 倍计分，摘要命中按 1 倍计分；同一关键词只计一次。
- `min_score`：低于该分数直接丢弃，用来压掉“顺带提到”的噪声。
- `color` / `icon` / `label`：只影响页面展示。

新增一个主题时，复制一个对象、改 `id`（必须唯一）、`label`、检索词和权重即可；页面会自动多出一个筛选标签。

> 调参建议：先看 `docs/data/papers.json` 里被丢弃/保留的标题，再微调 `min_score` 和 `require_groups`。宁可少而准，也不要让“submillimeter”匹配到微纳加工论文。

---

## 数据与去重逻辑

- **arXiv**：优先用简单查询 + `submittedDate` 窗口调用 arXiv API；如果 API 限流或超时，自动回退到 `https://rss.arxiv.org/rss/<分类>` 的当日 RSS。预印本始终视为 OA。
- **OpenAlex**：只保留 `is_oa:true`、`type:article`、发表日期在窗口内的记录；摘要由 `abstract_inverted_index` 还原。
- **速读总结**：从摘要中确定性抽取“开场句 + 最像结果句”的两句话，不调用大模型、不会凭空编造内容；页面上标注为“自动提取”。
- **去重**：优先用 DOI，其次 arXiv ID，最后归一化标题；同一篇论文的 arXiv 记录与期刊记录会合并，保留双方链接。
- **“今日新增”**：按 `first_seen`（首次进入归档的北京时间日期）统计，而不是论文发表日期。这样即使 OpenAlex 延迟索引，也能在入库当天看到。
- **滚动归档**：默认保留 60 天；页面默认显示最近 7 天，可切到 30 天或全部。
- **开放获取**：只聚合公开元数据和链接，不下载或托管出版商全文。PDF 链接指向 arXiv 或 OpenAlex 给出的 OA 位置。

---

## 目录结构

```text
.
├── .github/workflows/daily.yml   # 每日抓取 + GitHub Pages 部署
├── config/topics.json            # 主题、检索式、关键词权重
├── data/archive.json             # 滚动归档（自动生成）
├── index.html                    # 本地预览跳转页
├── docs/
│   ├── index.html                # 仪表盘（单文件，数据内嵌）
│   ├── data/papers.json          # 同一份结构化数据
│   └── digests/
│       ├── YYYY-MM-DD.md         # 每日 Markdown 日报
│       ├── index.md              # 日报归档目录
│       └── index.json            # 归档元数据
└── scripts/
    ├── fetch_papers.py           # 抓取、打分、合并、渲染
    └── template.html             # 仪表盘模板
```

---

## 常见问题

**页面没有数据 / 工作流失败？**
先看 Actions 日志。arXiv API 偶尔会返回 429/503，脚本会自动退避重试，并在 API 不可用时回退到 arXiv 每日 RSS；OpenAlex 也可能限流，但另一个源和已归档数据仍会保留。首次运行且所有源都失败时，脚本返回非零退出码。

**arXiv 的 RSS 和 API 有什么区别？**
RSS 只包含当天公告，适合每日增量；API 可以按 `submittedDate` 回看最近 N 天，适合首次回填。脚本两者都支持，默认先 API、后 RSS，并按 arXiv ID 去重。

**为什么 MKIDs 有时候一天只有 0–2 篇？**
这个方向本身产量不高，arXiv 是最稳定的来源；OpenAlex 只补充 OA 期刊记录。可以适当扩大 `--days` 或增加 `openalex_queries`，但不要盲目降低 `min_score`。

**为什么 OpenAlex 会在期刊论文里混入明显不相关的标题？**
OpenAlex 的 `search` 是相关性检索，不是严格布尔过滤。脚本会用 `require_groups` + `min_score` 再做本地过滤；如果仍有噪声，优先收紧 `require_groups`。

**能改成邮件推送 / RSS 吗？**
可以。`docs/data/papers.json` 和每日 Markdown 都是稳定输出；在 GitHub Actions 里增加一个发送邮件的步骤，或把 `docs/digests/*.md` 转成 RSS 即可。当前版本不包含邮件服务，避免引入第三方密钥。

**许可证？**
示例代码可自由修改用于个人或科研工作；引用元数据时请遵守 arXiv 与 OpenAlex 各自的使用条款。
