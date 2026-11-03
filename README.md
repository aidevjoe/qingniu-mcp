# 轻牛健康 MCP Server

把「轻牛健康 / 云麦 Yolanda」体脂秤的云端体测数据，以 **只读 MCP 工具** 的形式暴露出来，供任意支持 MCP 的客户端（Agent / 桌面端）读取并做健康、健身分析。

> 协议逆向自轻牛 Android 客户端 v4.21.1，**纯只读**：只能拉取你自己的云端体测记录。凭证（手机号 + 密码）只在运行本服务的机器上使用——密码本地做 MD5 后提交，与官方 App 一致；对外仅向 `gw.qnniu.com` 发请求。

## 1. 暴露的工具

| 工具 | 说明 |
| --- | --- |
| `qn_list_members` | 列出账号下所有成员，返回 `user_id`、`name`、记录数 |
| `qn_get_summary` | 每个成员最新体成分 + 窗口变化量（体重/体脂/肌肉/BMI/基础代谢等） |
| `qn_get_measurements` | 原始体测记录（时间升序），可按 `days`/`limit` 裁剪 |
| `qn_get_latest` | 每个成员最近一次完整体测记录 |

---

## 2. 在 Docker 上启动镜像

拿到镜像地址后，在 NAS / 服务器的 Docker 中启动。所有需要设置的项如下：

### 2.1 镜像（Image）
`ghcr.io/aidevjoe/qingniu-mcp:latest`

### 2.2 端口（Port / 端口映射）
- 容器内服务**固定监听 8801/TCP**。
- 映射：**宿主机 8801 → 容器 8801**（协议 TCP）。
- 图形界面：在「端口」/「端口映射」里填 `8801`（容器）→ `8801`（主机）；若选 **host 网络模式**则无需映射，自动占用主机 8801。

### 2.3 环境变量（Environment）—— 账号密码在这里填
| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `QN_PHONE` | ✅ | 轻牛/云麦 App 登录手机号 |
| `QN_PASSWORD` | ✅ | App 登录密码（**不是**短信验证码；只用扫码登录的话先在 App「设置 → 账号安全」设一个密码） |
| `MCP_PORT` | ❌ | 容器内监听端口，默认 `8801`（须与 3.2 的映射一致） |
| `MCP_HOST` | ❌ | 默认 `0.0.0.0` |
| `QN_TOKEN_FILE` | ❌ | token 持久化路径，默认 `/app/state/token.json` |
| `QN_TOKEN_TTL_DAYS` | ❌ | token 有效期（天），默认 `30` |
| `MCP_AUTH_TOKEN` | ❌ | 启用后 MCP 端点需 `Authorization: Bearer <值>` 才能访问（公网/反代暴露时建议开启） |

图形界面：在「环境变量」里逐项添加上面的 KEY=VALUE；或把从 `.env.example` 复制填好的 `.env` 文件，在「环境文件 / env file」处挂载进去。

### 2.4 数据卷（Volume，可选但推荐）
把宿主机一个目录挂载到容器 `/app/state`，用于持久化登录 token，避免容器重启后重新登录：
- 图形界面：「挂载」/「卷」里添加 `宿主机目录` → `/app/state`（读写）。
- 命令行见 3.5，或 `docker-compose.yml` 已配好 `./state:/app/state`。

### 2.5 一条命令启动（等价于上述所有设置）
```bash
docker run -d --name qingniu-mcp --restart unless-stopped \
  -p 8801:8801 \
  -e QN_PHONE=你的手机号 \
  -e QN_PASSWORD=你的密码 \
  -v /path/to/state:/app/state \
  ghcr.io/aidevjoe/qingniu-mcp:latest
```

---

## 3. 客户端连接

服务通过 HTTP（streamable-http）暴露 MCP。把 `mcp-config.example.json` 里的 `<NAS_IP>` 换成你服务可达地址（局域网 IP / VPN 地址 / 反代域名），合并进你的 MCP 客户端配置即可：

```
http://<NAS_IP>:8801/mcp
```

> 服务默认**不校验 token**，依赖私有网络访问控制。若需公网暴露，务必：① 在反向代理上启用 HTTPS + Basic Auth；② 设置 `MCP_AUTH_TOKEN` 并在客户端加 `Authorization: Bearer ...` 头。

---

## 4. 故障排查

| 现象 | 处理 |
| --- | --- |
| 登录失败 `[xxxx] ...` | 密码是 **App 登录密码**，不是验证码；只用扫码登录的话先在 App 设密码 |
| 端口起不来 | 检查 8801 是否被占用，或改 `MCP_PORT` 并同步改端口映射 |
| token 过期 / 一直重新登录 | 删 `state/token.json` 后重启容器 |
| 接口突然全失败 | 轻牛可能改版接口，需同步更新 `server.py` 里的协议常量 |
