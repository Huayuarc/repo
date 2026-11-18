# Huayuarc Repo

一个「GitHub 当门面，Cloudflare R2 当仓库」的越狱插件源，做法和莫忘源（mowang-repo）一样：

- **安装包**放在 Cloudflare R2，由 R2 的公开域名直连下载，不占 GitHub Pages 那 1 GB 的额度；
- **索引**（`Packages` / `Release`）由 GitHub Actions 自动生成并提交到仓库根目录，Pages 直接托管；
- **GitHub 上的 `debs/` 只是中转站**：传上来 → 验包 → 上 R2 → 重建索引 → 提交记录 → 删掉中转文件。

源地址：`https://huayuarc.github.io/repo/`

---

## 一、第一次使用前要做的事（只做一次）

### 1. 建一个 R2 存储桶

1. Cloudflare 控制台 → **R2** → **Create bucket**，名字例如 `huayuarc-debs`。
2. 进入桶 → **Settings** → **Public access**：
   - 想省事：开 **R2.dev subdomain**，得到形如 `https://pub-xxxxxxxx.r2.dev` 的地址；
   - 想要好看/更稳：绑定自己的域名（Custom domain），例如 `https://debs.huayuarc.com`。
   
   这个地址就是后面的 `R2_PUBLIC_URL`（**结尾不要带 `/`**）。
3. 桶 → **Settings** → **CORS** 可以不配；**Object lifecycle** 不要设自动删除。

> R2 免费额度：存储 10 GB、每月 100 万次写入、1000 万次读取，**下载流量免费**。用来做插件备份足够。

### 2. 建一个 R2 API 令牌

Cloudflare 控制台 → **R2** → **Manage API tokens** → **Create API token** → 权限选 **Object Read & Write**，范围限定在刚建的桶。
创建后会给你：

- Access Key ID
- Secret Access Key
- 还有页面上的 **Account ID**（在 R2 概览页右侧）

### 3. 在 GitHub 仓库里加配置

仓库 **Settings ▸ Secrets and variables ▸ Actions**：

| 类型 | 名字 | 值 |
| --- | --- | --- |
| Secret | `R2_ACCOUNT_ID` | Cloudflare Account ID |
| Secret | `R2_ACCESS_KEY_ID` | 令牌的 Access Key ID |
| Secret | `R2_SECRET_ACCESS_KEY` | 令牌的 Secret Access Key |
| Secret | `R2_BUCKET` | 桶名，例如 `huayuarc-debs` |
| Variable | `R2_PUBLIC_URL` | `https://pub-xxxxxxxx.r2.dev`（或你的自定义域名） |
| Variable | `SITE_URL` | `https://huayuarc.github.io/repo` |

> `R2_PUBLIC_URL` 也可以直接写进 `repo.json` 的 `publicBaseURL`，效果一样。

### 4. 打开 GitHub Pages

仓库 **Settings ▸ Pages** → **Source** 选 **Deploy from a branch** → 分支 **main**、目录 **/(root)** → 保存。

---

## 二、上传插件

1. 在 GitHub 网页打开仓库的 **`debs/`** → **Add file ▸ Upload files** → 选你的 `.deb` → **Commit changes**。
2. Actions 自动开始跑「发布插件」：
   - 读控制文件，检查 `Package / Version / Architecture / Description / Maintainer`，架构只收 `iphoneos-arm64`、`iphoneos-arm64e`、`iphoneos-arm`；
   - 上传到 R2（`debs/<Package>_<Version>_<Architecture>.deb`）；
   - 写 `packages/<Package>.json` 永久记录、`registry.json` 清单；
   - 重建 `Packages`、`Packages.gz`、`Release`；
   - 提交记录并从 GitHub 删掉刚才那个 deb；
   - 最后请求一次站点，确认新包已经能在索引里看到。
3. 一两分钟后，Sileo / Saily / Irisia 里刷新源就能看到新插件。

**任何一步失败，中转文件都不会被清理**，修好后重新触发一次（Actions ▸ 发布插件 ▸ Run workflow）即可，已上传的文件会自动跳过。

同一个 `Package + Version + Architecture` 不允许换内容重发：想重打就升版本号。

## 三、下架插件

1. 删除 `packages/<Package>.json`（GitHub 网页上删除文件并提交）。
2. Actions 会重建索引，插件立刻从软件源消失。
3. 对应的安装包不会马上删：最后一次引用消失后进入 **至少 48 小时** 保护期，期间每天巡检确认，**两次确认仍无引用** 才从 R2 真正删除（`sweep.yml`）。中途又把 JSON 加回来，保护期作废。

## 四、把旧源的插件搬过来

仓库自带的「从旧源迁移」工作流，会读旧源（`repo.json` 里的 `mirrorSource`，默认 `https://huayuarc.github.io`）的 `Packages`，把 deb 下载后传进 R2，并生成管理记录。

- Actions ▸ **从旧源迁移** ▸ Run workflow
- `all_versions`：默认只搬每个插件/架构的最新版，勾上则搬全部历史版本；
- `limit`：本次最多搬几个文件，旧包很多时可以先跑 20 个试试；
- `dry_run`：只列清单，不下载。

搬完以后，旧源可以停用，或者把旧源的 `debs/` 清掉——新源不再依赖它。

## 五、目录说明

```
debs/                 中转区：你把 deb 传到这里（发布成功后会被自动清空）
packages/<id>.json    每个插件的永久记录：版本、大小、SHA256、控制字段（删掉 = 下架）
registry.json         所有上传过、还在 R2 上的文件
orphans.json          已失去引用、正在 48 小时保护期的文件
scripts/repo.py       全部逻辑：publish / index / sweep / mirror / check / verify-site
repo.json             源的名称、说明、站点地址、旧源地址
index.html            源首页（添加源的按钮）
.github/workflows/    publish（发布）、sweep（保护期巡检）、migrate（迁移旧源）
Packages / Release    自动生成，不要手改
```

## 六、本地自检（可选）

在仓库目录里：

```bash
python3 scripts/repo.py index    # 从 packages/ 重建索引
python3 scripts/repo.py check    # 检查记录一致性
python3 scripts/repo.py sweep --dry-run   # 看哪些文件还在保护期
```

`publish` / `mirror` / `sweep` 需要上面那五个 R2 环境变量，本地跑之前先 `export`。
