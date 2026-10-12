# DuSync 服务端 — 免费公网部署指南（Render）

> 无需购买服务器，注册账号即可免费获得公网地址，手机随时随地都能连。

## 为什么选择 Render？

| 特性 | Render 免费版 |
|---|---|
| 费用 | 完全免费 |
| 运行时长 | 15 分钟无请求自动休眠，有请求时 30 秒内唤醒 |
| 磁盘 | 1 GB 持久化磁盘（存放 sync.db + 文件 blobs） |
| 域名 | 自动分配 `xxx.onrender.com` |
| 信用卡 | 不需要 |

> 适合：个人/家庭小范围同步、低频使用场景。如果要求 24 小时在线无延迟，建议升级付费或购买云服务器。

## 部署步骤（约 5 分钟）

### 1. 准备代码

把 `cloud/` 文件夹内的以下文件上传到你自己的 GitHub 仓库（新建一个公开仓库，上传即可）：

- `server.py`
- `mobile.html`
- `Dockerfile`
- `render.yaml`

> 如果没有 GitHub 账号，也可以直接用 Render 的 "Upload Files" 功能上传 ZIP。

### 2. 注册并连接 Render

1. 打开 [render.com](https://render.com)，用 GitHub 账号一键注册/登录
2. 进入 Dashboard → 点击 "New +" → "Web Service"
3. 选择你刚创建的 GitHub 仓库，Render 会自动识别 `render.yaml`
4. 点击 "Create Web Service"，等待构建完成（约 2-3 分钟）

### 3. 获取公网地址

部署成功后，Render 会分配一个地址：
```
https://dusync-server-xxx.onrender.com
```

把这个地址填到客户端（电脑端/手机端）的「服务器地址」栏即可使用。

### 4. 客户端连接

- **电脑端**：打开 `client_gui.exe`，服务器地址填 `https://dusync-server-xxx.onrender.com`，账号密码随便填（首次自动注册）
- **手机端**：浏览器访问 `https://dusync-server-xxx.onrender.com`，用同一账号密码登录

> 注意：Render 免费版休眠后首次访问会等待 30 秒左右唤醒，这是免费方案的正常现象。

## 常见问题

**Q: 数据会丢失吗？**
A: 不会。`render.yaml` 已配置 1GB 持久化磁盘挂载到 `/data`，sqlite 数据库和上传的文件都会保存在磁盘上。

**Q: 为什么手机端显示 "Failed to fetch"？**
A: 服务端可能正在休眠中，等待 30 秒后再刷新；或者服务端构建失败，请到 Render Dashboard 查看 Build/Runtime Logs。

**Q: 可以同时在多个设备登录吗？**
A: 可以。同一账号支持多台设备同时在线，互踢下线问题已在服务端修复。

**Q: 磁盘满了怎么办？**
A: 免费版只有 1GB。如果满了，需要删除旧文件或升级到付费版（5 美元/月起，包含 10GB 磁盘）。
