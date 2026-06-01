# feishu-doc-down

一个最小命令行工具：用飞书 `user_access_token` 枚举当前用户“我的空间”里的文件夹和文件，把在线文档导出、本地文件下载到指定目录。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 使用

先授权一次，扫码后会把 `user_access_token` 保存到本机配置文件：

```bash
export FEISHU_APP_ID='cli_xxxx'
export FEISHU_APP_SECRET='xxxx'
feishu-doc-down auth
```

也可以把应用信息保存到本机配置文件，之后不用每次 export：

```bash
feishu-doc-down auth app-config \
  --app-id 'cli_xxxx' \
  --app-secret 'xxxx'

feishu-doc-down auth
```

如果没有环境变量、也没有保存过应用信息，直接运行 `auth` 会提示输入 App ID / App Secret，并自动保存到本机：

```bash
feishu-doc-down auth
```

然后下载：

```bash
feishu-doc-down ./downloads
```

`auth` 命令授权成功后会正常退出；它只负责保存 token，不会自动开始下载。

也可以用交互菜单选择下载来源：

```bash
feishu-doc-down menu
```

菜单里可以选：

```text
1. 云盘
2. 我的文档库
3. 全部
4. 搜索
5. 按链接下载
6. 导出日历日程
```

默认下载来源是“我的文档库”，对应飞书 Web 左侧“我的文档库”侧栏：

```bash
feishu-doc-down ./downloads --source my-library
```

如果想把“我的文档库”和云盘目录都抓一遍：

```bash
feishu-doc-down ./downloads --source all
```

如果想用搜索视角下载当前用户可搜索到的云文档：

```bash
feishu-doc-down ./downloads --source search --search-key 文档
```

如果列表接口漏掉了 Web 侧栏里的某个文档，可以复制文档链接直接下载：

```bash
feishu-doc-down ./downloads --url 'https://xxx.feishu.cn/docx/xxxxx'
```

多个链接可以重复传：

```bash
feishu-doc-down ./downloads \
  --url 'https://xxx.feishu.cn/docx/xxxxx' \
  --url 'https://xxx.feishu.cn/base/xxxxx'
```

检查当前是否已经保存 token：

```bash
feishu-doc-down auth status
```

检查当前 token 对应的是哪个飞书用户：

```bash
feishu-doc-down auth whoami
```

默认回调地址是：

```text
http://127.0.0.1:8765/callback
```

你需要先在飞书开发者后台把这个地址加入应用的“开发配置 -> 安全设置 -> 重定向 URL”。如果你想换端口或路径：

```bash
feishu-doc-down auth --redirect-uri http://127.0.0.1:9000/callback
```

注意：默认 `127.0.0.1` 回调适合在电脑浏览器打开授权页；如果直接用手机扫终端里的二维码，授权后的回调会访问手机自己的 `127.0.0.1`，本机 CLI 收不到。更稳的做法是让命令自动打开电脑浏览器，然后在飞书授权页里扫码登录。若必须用手机直接扫终端二维码，需要把 `--redirect-uri` 设置成这台电脑在手机可访问的地址，例如局域网 IP 或内网穿透地址，并把同一个地址配置到飞书应用的重定向 URL。

### 授权报错：错误码 20029

`重定向 URL 有误` / `错误码：20029` 表示命令生成的 `redirect_uri` 没有精确匹配飞书应用后台配置的重定向 URL。

默认命令使用：

```text
http://127.0.0.1:8765/callback
```

请检查飞书开发者后台里同一个应用的重定向 URL 是否逐字符一致：

- `http` 和 `https` 必须一致。
- `127.0.0.1` 和 `localhost` 不算同一个地址。
- 端口 `8765` 必须一致。
- 路径 `/callback` 必须一致。
- 末尾不要多一个 `/`。
- `FEISHU_APP_ID` 必须来自你正在配置重定向 URL 的同一个应用。

如果后台配置的是另一个地址，运行 `auth` 时也要传同一个地址：

```bash
feishu-doc-down auth --redirect-uri 'http://localhost:8765/callback'
```

如果你不想保存 token，也可以直接用现成的 token：

```bash
export FEISHU_USER_ACCESS_TOKEN='u-xxxx'
feishu-doc-down ./downloads
```

也可以直接传 token：

```bash
feishu-doc-down ./downloads --token 'u-xxxx'
```

常用参数：

```bash
feishu-doc-down ./downloads \
  --source my-library \
  --url 'https://xxx.feishu.cn/docx/xxxxx' \
  --doc-format docx \
  --sheet-format xlsx \
  --bitable-format xlsx \
  --skip-existing
```

海外 Lark 域名：

```bash
feishu-doc-down ./downloads --base-url https://open.larksuite.com
```

先只看会下载什么，不真正下载：

```bash
feishu-doc-down ./downloads --dry-run
```

## 导出日历

导出主日历某个时间范围内的日程：

```bash
feishu-doc-down calendar ./calendar-events.csv \
  --start 2026-05-01 \
  --end 2026-06-01 \
  --format csv
```

支持格式：

```text
csv
json
ics
```

导出全部可读日历：

```bash
feishu-doc-down calendar ./calendar-export \
  --all-calendars \
  --start 2026-05-01 \
  --end 2026-06-01 \
  --format csv
```

指定日历 ID：

```bash
feishu-doc-down calendar ./calendar-events.ics \
  --calendar-id 'feishu.cn_xxx@group.calendar.feishu.cn' \
  --start 2026-05-01T00:00:00+08:00 \
  --end 2026-06-01T00:00:00+08:00 \
  --format ics
```

说明：飞书 `calendar/v4/calendars/:calendar_id/events` 按 `start_time` / `end_time` 查询时不分页；如果时间范围太大，服务端可能按 `page_size` 截断。建议按月或按季度导出。

## 打包客户端

本地打包当前平台的单文件命令行客户端：

```bash
python -m pip install ".[build]"
python -m PyInstaller --onefile --name feishu-doc-down --hidden-import qrcode feishu_doc_down/cli.py
```

产物在 `dist/`：

```bash
./dist/feishu-doc-down menu
```

Windows 上产物是：

```powershell
dist\feishu-doc-down.exe menu
```

仓库内置 GitHub Actions：打 `v*` tag 或手动运行 `build-binaries` workflow，会分别生成：

- `feishu-doc-down-linux-x64`
- `feishu-doc-down-windows-x64`

## 需要的 token 和权限

这个工具使用用户身份的 `user_access_token`，因为应用身份一般看不到用户自己的“我的空间”资源。

常见需要开通并授权的权限：

- 获取“我的空间”根目录元信息：`drive:drive.metadata:readonly` 或 `drive:drive`
- 获取 Explorer v2 文件夹下文档清单：`drive:drive`
- 获取 drive v1 文件夹内文件清单：`drive:drive:readonly`
- 搜索云文档：`search:docs:read`
- 获取“我的文档库”节点列表：`wiki:wiki:readonly` 或 `wiki:node:retrieve`
- 读取日历列表/主日历：`calendar:calendar:read`
- 读取日程列表：`calendar:calendar.event:read`
- 下载普通云空间文件：`drive:file:download`
- 导出在线文档：`docs:document:export`
- 刷新 token：`offline_access`
- OAuth 用户身份基础授权：`auth:user.id:read`

不同租户后台显示的权限名称可能不同，以飞书开放平台当前页面为准。

建议一次性申请并授权这些 scope：

```text
auth:user.id:read drive:drive drive:drive.metadata:readonly drive:drive:readonly search:docs:read wiki:wiki:readonly wiki:node:retrieve calendar:calendar:read calendar:calendar.event:read drive:file:download docs:document:export offline_access
```

如果你的租户权限 key 不一样，可以显式传入：

```bash
feishu-doc-down auth --scope 'auth:user.id:read drive:drive drive:drive.metadata:readonly drive:drive:readonly search:docs:read wiki:wiki:readonly wiki:node:retrieve calendar:calendar:read calendar:calendar.event:read drive:file:download docs:document:export offline_access'
```

`drive:drive` 包含编辑/管理能力，范围明显更大；但飞书历史版 Explorer v2 的“获取文件夹下文档清单”接口明确要求这个权限。

授权结果保存到 `~/.config/feishu-doc-down/token.json`，文件权限会设置为 `0600`。这个文件里包含 access token、refresh token 和 App Secret，不要提交到仓库或发给别人。

应用信息可保存到 `~/.config/feishu-doc-down/app.json`，用于在没有 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 环境变量时自动授权。

Windows 打包版会保存到 `%APPDATA%\feishu-doc-down\token.json`。

### sudo 下读不到环境变量

Linux/macOS 上不要用 `sudo` 运行授权命令。`sudo` 默认会清空当前 shell 里的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，还会把 token 保存到 root 用户目录。

正确方式：

```bash
chmod +x ./feishu-doc-down-linux-x64
export FEISHU_APP_ID='cli_xxxx'
export FEISHU_APP_SECRET='xxxx'
./feishu-doc-down-linux-x64 auth
```

如果文件名是 `feishu-doc-down`：

```bash
chmod +x ./feishu-doc-down
./feishu-doc-down auth
```

只有在明确知道后果时才使用：

```bash
sudo -E ./feishu-doc-down auth
```

飞书新版 OAuth 返回的 `user_access_token` 不一定以 `u-` 开头，只要它来自上述授权流程即可。

### 下载报错：错误码 99991661

`Missing access token for authorization` 表示下载请求没有带上可用的用户 token。先检查 CLI 读到的配置：

```bash
feishu-doc-down auth status
```

如果显示 `access_token: missing`，重新授权：

```bash
feishu-doc-down auth
```

如果你授权时用了自定义配置路径，下载时也要传同一个 `--config`：

```bash
feishu-doc-down auth --config ./token.json
feishu-doc-down ./downloads --config ./token.json
```

如果你是直接传 token，确认环境变量不是空字符串，或者直接用 `--token`：

```bash
feishu-doc-down ./downloads --token '你的_user_access_token'
```

### 下载报错：错误码 99991679

`应用未获取所需的用户授权：[drive:drive, drive:drive.metadata:readonly]` 表示应用后台或用户授权缺少云空间元数据读取权限。

处理方式：

```bash
feishu-doc-down auth --scope 'auth:user.id:read drive:drive drive:drive.metadata:readonly drive:drive:readonly search:docs:read wiki:wiki:readonly wiki:node:retrieve calendar:calendar:read calendar:calendar.event:read drive:file:download docs:document:export offline_access'
feishu-doc-down ./downloads
```

如果重新授权仍然报错，先到飞书开发者后台给同一个应用开通错误提示里的 scope，然后重新发布/生效权限，再重新运行上面的 `auth`。

## 支持范围

- 默认 `--source my-library` 使用 `GET /open-apis/wiki/v2/spaces/my_library/nodes` 递归遍历“我的文档库”。
- `--source all` 会下载“我的文档库”和云盘目录；如果同时传 `--search-key`，也会下载搜索结果。
- `doc` / `docx` 导出为 `docx` 或 `pdf`，默认 `docx`。
- `sheet` / `bitable` 导出为 `xlsx` 或 `csv`，默认 `xlsx`。
- `file` 类型按原文件下载。
- `--source folder` 使用新版 `GET /open-apis/drive/v1/files` 递归遍历“我的空间”文件夹树；部分租户/空间形态下根目录可能返回空。
- `--source explorer` 使用 `GET /open-apis/drive/explorer/v2/folder/:folderToken/children` 递归遍历“我的空间”文件夹树，并在本地创建同名目录。
- `--source search` 通过云文档搜索下载当前用户可搜索到的文档，保存为扁平文件列表。
- `--url` 支持按链接直接下载 `docx` / `doc` / `sheets` / `base` / `bitable`。
- Explorer v2 返回的快捷方式如果包含目标 token/type，会直接按目标文档下载。
- drive v1 返回的 `shortcut` 默认跳过，可用 `--include-shortcuts` 尝试按目标 token 下载。

限制：

- `--source my-library` 对应 Web 侧栏的“我的文档库”。
- `--source explorer` / `--source folder` 只看云盘“我的空间”文件树，不等同于客户端“最近”或“共享给我”列表。
- `--source search` 是搜索视角，可能包含他人共享给你的文档。
- 飞书的导出任务产物需要在任务完成后及时下载，工具会立即下载。
- `csv` 导出表格/多维表格通常需要指定子表 ID，本工具默认使用 `xlsx` 避免这个额外参数。
- 知识库 wiki 不在“我的空间”文件夹树里时不会被枚举；如果它以文件/快捷方式出现在树里，能否下载取决于飞书接口返回的类型和当前用户权限。

## 参考接口

- 获取我的空间 root folder 元信息：`GET /open-apis/drive/explorer/v2/root_folder/meta`
- 获取我的文档库节点列表：`GET /open-apis/wiki/v2/spaces/my_library/nodes`
- 获取 Explorer v2 文件夹下文档清单：`GET /open-apis/drive/explorer/v2/folder/:folderToken/children`
- 获取文件夹中的文件清单：`GET /open-apis/drive/v1/files`
- 搜索云文档：`POST /open-apis/suite/docs-api/search/object`
- 创建导出任务：`POST /open-apis/drive/v1/export_tasks`
- 查询导出任务：`GET /open-apis/drive/v1/export_tasks/:ticket`
- 下载导出文件：`GET /open-apis/drive/v1/export_tasks/file/:file_token/download`
- 下载普通文件：`GET /open-apis/drive/v1/files/:file_token/download`
- 获取主日历：`POST /open-apis/calendar/v4/calendars/primary`
- 获取日历列表：`GET /open-apis/calendar/v4/calendars`
- 获取日程列表：`GET /open-apis/calendar/v4/calendars/:calendar_id/events`
