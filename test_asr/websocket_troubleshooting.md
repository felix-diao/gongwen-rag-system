# WebSocket 连接问题排查指南（code=1006）

## 问题描述
前端实时录音时，WebSocket 连接失败，显示 `code=1006`，提示"反向代理未开启 WebSocket Upgrade / 后端不可达"。

## 可能原因

### 1. 反向代理（Nginx）未配置 WebSocket 升级
如果使用 Nginx 作为反向代理，需要添加以下配置：

```nginx
location /api/minutes {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    
    # WebSocket 升级配置
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    
    # 超时设置
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
}
```

### 2. 开发环境 UmiJS 代理问题
检查 `config/proxy.ts` 中的配置是否正确：

```typescript
'/api/minutes': {
  target: ragTarget,  // 确保指向正确的后端地址
  changeOrigin: true,
  ws: true,  // 必须设置为 true
  pathRewrite: { '^/api/minutes': '/api/minutes' }
}
```

### 3. 后端服务状态检查
- 确认后端服务正在运行：`ps aux | grep uvicorn`
- 确认端口监听：`netstat -tlnp | grep 8080`
- 测试后端健康检查：`curl http://127.0.0.1:8080/health`

### 4. 网络连接问题
- 检查防火墙是否阻止 8080 端口
- 检查前端是否能访问后端 API（非 WebSocket）
- 检查浏览器控制台的 Network 标签，查看 WebSocket 连接详情

## 快速诊断步骤

1. **检查后端服务是否运行**
   ```bash
   ps aux | grep uvicorn
   ```

2. **检查后端日志**
   查看后端服务的日志输出，看是否有 WebSocket 连接请求

3. **检查前端控制台**
   - 打开浏览器开发者工具
   - 查看 Network -> WS 标签
   - 查看 WebSocket 连接的详细信息

4. **测试直接连接**
   在浏览器控制台测试：
   ```javascript
   const ws = new WebSocket('ws://127.0.0.1:8080/api/minutes/volc/1/live?token=YOUR_TOKEN');
   ws.onopen = () => console.log('连接成功');
   ws.onerror = (e) => console.error('连接失败', e);
   ws.onclose = (e) => console.log('连接关闭', e.code, e.reason);
   ```

## 解决方案

### 方案1：如果是开发环境
确保前端开发服务器正在运行，并且代理配置正确。

### 方案2：如果是生产环境且有 Nginx
添加上述 Nginx 配置并重启 Nginx：
```bash
sudo nginx -t  # 测试配置
sudo nginx -s reload  # 重载配置
```

### 方案3：如果直接连接后端
确保：
- 后端服务监听 `0.0.0.0:8080`（不是 `127.0.0.1`）
- 防火墙允许 8080 端口
- 前端使用正确的后端地址

## 后端代码检查
后端 WebSocket 路由已正确配置在：
- 文件：`app/api/meeting_minute_volc.py`
- 路由：`/api/minutes/volc/{meeting_id}/live`
- 已注册到 FastAPI 应用

## 前端代码检查
前端 WebSocket 连接代码在：
- 文件：`src/pages/Meetings/Minutes/index.tsx`
- 函数：`startVolcLiveRecording()` (第660行)
- URL 构建：`buildWsUrl()` (第283行)

---

## 录音上传 502 排查（POST /api/minutes/volc/{meeting_id}/upload）

### 现象
`POST /api/minutes/volc/13/upload` 返回 `502 Bad Gateway`，提示「上传至对象存储失败」。

### 排查步骤

1. **查看接口返回的 detail**
   响应体中的 `detail` 字段会包含具体异常，例如：
   - `VOLC_TOS_BUCKET is not configured` → TOS 未配置
   - `ve-tos-python-sdk is required` → 未安装 TOS SDK
   - `Connection refused` / `Timeout` → 网络或 TOS 不可达

2. **检查后端日志**
   ```bash
   # 查看 uvicorn 输出，会有完整 traceback
   tail -f /path/to/your/uvicorn.log
   ```

3. **检查 TOS 配置（.env）**
   ```env
   VOLC_TOS_BUCKET=你的桶名
   VOLC_TOS_ENDPOINT=https://tos-cn-beijing.volces.com
   VOLC_TOS_REGION=cn-beijing
   VOLC_TOS_ACCESS_KEY_ID=你的AccessKey
   VOLC_TOS_SECRET_ACCESS_KEY=你的SecretKey
   ```

4. **确认 TOS SDK 已安装**
   ```bash
   pip install ve-tos-python-sdk
   # 或 requirements.txt 中的 tos
   pip install -r requirements.txt
   ```

5. **网络与代理**
   若本机有 HTTP 代理，需在 `.env` 或环境变量中设置：
   ```env
   NO_PROXY=volces.com,bytedance.com,openspeech.bytedance.com
   ```
