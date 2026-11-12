# send-me

[English](README.md) · **中文**

用一次性链接接收朋友发来的文件。文件直接经 Cloudflare Tunnel 传到你自己的电脑上：不需要公网 IP，发送方不用注册账号，平时也没有任何服务在后台运行。

```
just link   →   https://send.example.com/k3J9…   →   朋友上传   →   链接失效
```

## 工作原理

```
 朋友的浏览器 ──HTTPS──▶ Cloudflare ──隧道──▶ cloudflared ──▶ send-me.py ──▶ ~/Downloads/send-me/
```

- `just link` 启动一个小型 Python 服务（只用标准库）和一条 `cloudflared` 隧道，打印链接和二维码。
- 第一个开始上传的浏览器会占用这个链接，其他人打开会看到“链接已被使用”。
- 上传完成或链接过期后，服务自动退出，隧道随之关闭。每次运行都会生成新的随机 token，旧链接永远无法再用。

## 功能

- **一次性链接**：每次运行生成 24 位随机 token，可以设置过期时间和总大小上限。
- **并行分块上传**：文件切成 8 MiB 的块，默认 4 个连接同时传（可调），在丢包较多的跨境线路（比如中国大陆）上提速明显。
- **断点续传（类似 `rsync --partial`）**：
  - 取消上传、刷新页面、网络中断，都会从中断处继续。
  - 程序被停掉或链接过期后，重新运行 `just link`，让对方选同样的文件，已收到的块会被跳过。
  - 未完成的数据保存在 `~/Downloads/send-me/.incoming/`，保留 7 天。
- **端到端校验**：浏览器为每块计算 SHA-256，服务器收到后核对，校验不通过的块会自动重传。
- **终端实时进度**：进度条、已收 / 总大小、速度、剩余时间、当前文件。
- **网页**：
  - 支持拖拽和多选文件。
  - 每个文件有单独进度，另有环形总进度，可以取消和继续。
  - 支持浅色 / 深色模式，背景是 WebGL 流体动画，界面中英双语。
  - 不加载任何外部资源，在国内也能正常打开。
- **链接容错**：聊天软件把文字粘到了链接后面（如 `…/TOKEN，谢谢`），也能正常打开。

## 环境要求

- Linux 或 macOS，装有 [Nix](https://nixos.org)（需启用 flakes）和 [direnv](https://direnv.net)（推荐 nix-direnv）。
  开发环境会提供 `cloudflared`、`just`、`uv` 和 `qrencode`。
- 不用 Nix 的话：自己安装 `cloudflared`、`just` 和 `uv`（`qrencode` 可选）。
- 如果想要固定网址：需要一个 DNS 托管在 Cloudflare 的域名（免费套餐即可）。

## 快速开始

```sh
cd send-me
direnv allow          # 仅首次：构建开发环境
just link             # 打印链接和二维码，等待上传
```

把链接发给朋友，收到的文件会保存在 `~/Downloads/send-me/<日期-时间>/`。
没有配置域名时，`just link` 会使用随机的 `*.trycloudflare.com` 临时隧道。临时隧道不需要账号，但 Cloudflare 不保证它的可用性。

## 使用自己的域名（推荐）

只需设置一次。运行后会弹出浏览器，登录 Cloudflare 并选择你的域名：

```sh
just setup send.example.com
```

它会依次执行 `cloudflared tunnel login`，创建名为 `send-me` 的隧道，添加 DNS 记录，并写入 `.env`：

```sh
SEND_ME_TUNNEL=send-me
SEND_ME_HOSTNAME=send.example.com
```

之后 `just link` 会打印 `https://send.example.com/<token>`：网址固定，token 每次都不同。send-me 没在运行时，这个域名上没有任何服务响应。

## 命令

| 命令 | 说明 |
|---|---|
| `just link [参数]` | 生成一次性上传链接（如有 `.env` 则使用自己的域名） |
| `just quick [参数]` | 强制使用随机的 trycloudflare.com 链接 |
| `just local [参数]` | 仅本机访问，不开隧道（用于测试） |
| `just setup <域名>` | 一次性配置自己域名的固定隧道 |
| `just teardown` | 删除隧道和 `.env`（DNS 记录需在控制台手动删除） |
| `just open` | 打开下载目录 |
| `just clean` | 删除为续传保留的未完成数据 |
| `just help` | 查看所有参数 |

参数（写在 `just link` 后面，例如 `just link -e 2 -m 10 -j 8`）：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `-e, --expire 小时` | `24` | 链接有效期 |
| `-m, --max-gb GB` | `50` | 总大小上限，`0` 为不限 |
| `-j, --parallel N` | `4` | 同时上传的块数（1–16） |
| `-o, --out 目录` | `~/Downloads/send-me` | 保存位置 |
| `-p, --port 端口` | 随机 | 本地端口 |
| `--tunnel / --hostname` | 读取 `.env` | 固定隧道名和对应域名 |

## 续传被中断的上传

1. 终端会显示 `kept for resume: <文件名> (已收 / 总大小)`。
2. 重新运行 `just link`，把新链接发给对方。
3. 对方打开链接，选择**同样的文件**（文件名、大小和修改时间都要一致）。
4. 页面提示“已从断点继续”，只会上传缺少的部分。

## 安全说明

- 只有拿到完整链接的人才能上传，而且只能由一个浏览器会话使用。
- 文件名会被清理，防止路径穿越和隐藏文件；总大小受 `--max-gb` 限制。
- 浏览器到 Cloudflare 走 HTTPS，Cloudflare 到你的电脑走加密隧道。TLS 会在 Cloudflare 解密，所以不信任 Cloudflare 的数据不要用它传。
- 服务只监听 `127.0.0.1`。不运行 `just link` 时，没有任何东西对外暴露。

## 常见问题

- **对方打开是 404**：可能是聊天软件改动了链接，或者对方打开的是旧链接。终端会把每个 404 请求的 IP、国家、路径和浏览器记录下来。建议链接单独发一条消息，或者直接发二维码截图。
- **`just setup` 在浏览器提示“可以关闭此窗口”后卡住**：按 Ctrl+C，再运行一次即可。登录轮询偶尔会卡住。
- **出现 `WRN Your version … is outdated`**：不影响使用，只是 nixpkgs 里的版本比官方落后几个小版本。
- **远距离上传很慢**：试试 `-j 8`。如果速度没变化，瓶颈在对方自己的上行带宽。

## 文件说明

```
send-me.py    服务端、隧道管理、终端界面（仅用 Python 标准库，通过 `uv run --script` 运行）
upload.html   上传页面（单个自包含文件）
justfile      常用命令
flake.nix     开发环境：cloudflared、just、uv、qrencode
.envrc        direnv 的 `use flake`
.env          固定隧道配置（由 `just setup` 生成，已加入 .gitignore）
```
