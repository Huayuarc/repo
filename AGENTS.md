# AGENTS.md

仓库：`Huayuarc/repo`（https://github.com/Huayuarc/repo），源地址 `https://huayuarc.github.io/repo/`。
发布走的流程：GitHub `debs/` 中转 → Actions 上传 Cloudflare R2 → 重建索引并提交 → 删除中转文件。
旧源是 `Huayuarc/huayuarc.github.io`（约 7 GB，不要克隆），迁移用 `migrate.yml`。

这是一个 **Debian/APT 软件源仓库**（越狱插件源），不是 tweak 项目：没有 `apelin-project.json` 的构建描述，
也不需要 Theos 或任何编译器。所有工作都在 Python 脚本和 GitHub Actions 里。

## 结构

- `debs/`：中介上传区。用户把 `.deb` 传到这里，Action 处理后自动删除。
- `packages/<Package>.json`：每个插件的永久记录（版本数组，每项带 control 字段、size、sha256、url）。删除它 = 下架。
- `registry.json` / `orphans.json`：R2 上的文件清单、失去引用后的保护期状态。
- `scripts/repo.py`：全部逻辑，子命令 `publish | index | verify-site | sweep | mirror | check`。
- `Packages`、`Packages.gz`、`Release`：由 `index` 生成，别手改。
- `.github/workflows/`：`publish.yml`（push 到 main 触发）、`sweep.yml`（每天巡检）、`migrate.yml`（手动迁移旧源）。
- `repo.json`：源名、说明、`siteURL`、`mirrorSource`、可选 `publicBaseURL`。

## 常用命令

```bash
python3 scripts/repo.py index             # 重建索引（不需要网络）
python3 scripts/repo.py check             # 记录一致性自检
python3 scripts/repo.py sweep --dry-run   # 只报告保护期状态
python3 scripts/repo.py publish --dry-run # 只校验 debs/ 里的 deb
```

`publish` / `sweep` / `mirror` 需要 `R2_ACCOUNT_ID`、`R2_ACCESS_KEY_ID`、`R2_SECRET_ACCESS_KEY`、
`R2_BUCKET`、`R2_PUBLIC_URL`，脚本用 boto3 走 R2 的 S3 接口。

## 约定

- 包的 `Filename` 是 **R2 绝对地址**；旧源搬来的历史条目保留相对地址 `debs/xxx.deb`，两种都合法，不要"修正"。
- 同 `Package+Version+Architecture` 不允许换内容：publish 会拒绝并让人升版本。
- 提交信息带 `[skip ci]`，避免机器人自己的提交再次触发工作流。
- `Release` 只列 `Packages` 与 `Packages.gz`（不生成 xz/lzma），和旧仓库的做法保持一致，避免 CDN 缓存哈希不一致。
- 站点是 branch 部署（main / 根目录），仓库里提交的 `Packages`/`Release` 就是线上内容。

## 注意

本机的 shell `git` 在项目长路径上会截断（128 字节缓冲），只能用 Apelin 的 git 工具；Python、dpkg-deb、unzip 正常。
