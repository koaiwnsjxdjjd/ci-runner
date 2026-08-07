# ghbox（GitHub Box）— GitHub Actions 多实例管理器

利用 **GitHub Actions 临时环境** + **GitHub Releases 加密存储**，实现：
- **一键创建多个云端工作实例**（纯 API）
- **多 GitHub 账号支持**（一个账号 20 并发，N 个账号 = N×20）
- 每个实例：WSS 交互式终端 + API 命令执行 + 文件持久化 + **进程持久化** + 自动续命
- **进程持久化**：容器销毁前自动备份用户进程（命令+文件+环境），新实例启动自动无缝拉起
- 生产级模块化架构（core / manager / worker / process 分层）

## 架构

```
【管理实例 manager】总管家（ghvps2.kekeke.cc.cd）
  ├─ 账号池管理（多账号 + 自动负载均衡 + 并发检测）
  ├─ 实例创建/关闭/查询 API
  ├─ 健康监控 + 自动恢复 + 配额预警
  └─ 保命（Actions 故障自动切 Codespaces）

【工作实例 worker × N】
  ├─ WSS 交互式终端（bytes 传输无乱码 + pyte 干净屏幕）
  ├─ API 命令执行（带超时）
  ├─ 文件持久化（~/files 目录）
  ├─ ★ 进程持久化（用户进程自动备份/恢复/守护）
  ├─ 系统配置备份恢复
  └─ 自动续命（除非手动关闭）

【进程持久化 worker/process/】
  ├─ scanner.py  进程扫描识别（过滤系统/自身/隧道）
  ├─ config.py   进程配置读写（ghvps.json）
  ├─ backup.py   快照与文件备份
  ├─ restore.py  恢复/启动/停止/重启
  ├─ manager.py  门面聚合
  └─ api.py      进程管理 API
```

## 快速开始

### 1. 配置 Secrets（管理仓库）
| Secret | 说明 |
|--------|------|
| `GH_TOKEN` | 管理账号 GitHub Token |
| `DEMO_KEY` | AES-256 加密密钥（hex 64位） |
| `EXEC_TOKEN` | 远程控制/终端令牌 |
| `TUNNEL_TOKEN` | Manager 固定隧道凭证 |
| `CF_EMAIL` / `CF_API_KEY` | Cloudflare 账号 |
| `CF_ACCOUNT_ID` / `CF_ZONE_ID` | Cloudflare 区域 |

### 2. 触发管理实例
```bash
gh workflow run manager.yml --repo <owner>/demo-vps
```

### 3. 添加工作账号
```bash
curl -X POST https://ghvps2.kekeke.cc.cd/api/accounts \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $EXEC_TOKEN" \
  -d '{"name":"acc1","token":"ghp_xxx","repo":"<owner>/demo-vps","max_concurrency":20}'
```

### 4. 一键创建实例
```bash
curl -X POST https://ghvps2.kekeke.cc.cd/api/instances \
  -H "Authorization: Bearer $EXEC_TOKEN"
```

### 5. 连接终端
```bash
python3 -m cli.ghss_cli <EXEC_TOKEN> https://inst1.kekeke.cc.cd
```

## 进程持久化

在实例终端启动任意服务（node/python/cloudflared 等），销毁前自动备份，新实例自动恢复。

**配置文件 `ghvps.json`**（放在项目目录，可选）：
```json
{
  "name": "my-web",
  "command": "node server.js",
  "cwd": "/home/runner/files/my-web",
  "env": {"PORT": "3000"},
  "install": ["npm install", "npm run build"],
  "exclude": ["node_modules", ".git", "logs"],
  "auto_restart": true,
  "restart_delay": 3
}
```

**进程管理 API**：
| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/processes` | GET | 列出持久化进程 |
| `/api/processes/snapshot` | POST | 手动触发快照 |
| `/api/processes/<name>/restart` | POST | 重启进程 |
| `/api/processes/<name>/stop` | POST | 停止进程 |
| `/api/processes/<name>/start` | POST | 启动进程 |
| `/api/processes/<name>/log` | GET | 查看进程日志 |

## 管理 API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/status` | GET | 查看账号和实例总览 |
| `/api/overview` | GET | 完整数据总览 |
| `/api/accounts` | GET/POST | 查看/添加账号 |
| `/api/accounts/<name>` | DELETE | 删除账号 |
| `/api/instances` | POST/GET | 创建/查看实例 |
| `/api/instances/<id>` | GET/DELETE | 查看/关闭实例 |
| `/api/instances/<id>/exec` | POST | 在实例上执行命令 |
| `/api/logs` | GET | 查看日志 |
| `/api/resource` | GET | 资源监控 |

## 工作实例 API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/exec` | POST | 命令执行（token + timeout） |
| `/api/term/screen` | GET | pyte 干净屏幕文本 |
| `/api/backup` | POST | 手动备份 |
| `/api/status` | GET | 实例状态 |
| `/api/processes` | GET | 进程持久化管理 |
| `/socket.io` | WSS | 交互式终端 |

## 持久化

- 数据库 + `~/files/` 文件目录，每 120 秒加密备份到 Releases
- 每个实例数据独立（按实例 ID 隔离 asset）
- **进程持久化**：扫描用户进程 → 备份命令/文件/环境 → 新实例自动恢复
- job 销毁自动恢复，除非手动关闭实例