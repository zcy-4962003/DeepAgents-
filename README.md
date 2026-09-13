# 深度研搜多 Agent 系统

面向**公司内部员工**的智能研究报告生成系统。用户用自然语言提问，系统调度多个子智能体
汇集网络公开信息、业务数据库、本地知识库三路资料，产出结构化的 Markdown 研究报告，
并按需转换为 PDF。

系统为**单租户**部署（服务于一家公司的内部员工），但账号之间严格隔离：每个员工只能看到
自己的任务、会话、文件与长期记忆，管理员可查看全员数据与审计日志。

---

## 功能特性

| 能力 | 说明 |
|---|---|
| 多 Agent 协同 | 主智能体负责规划与成文，三个子智能体分别对接网络搜索、业务数据库、本地知识库 |
| 任务队列 | Redis + arq 异步执行，支持排队、重试（指数退避）、超时、失败恢复 |
| 实时进度 | 执行过程以事件流实时推送到浏览器，刷新页面后从数据库回放完整轨迹 |
| 对象存储 | 上传附件与生成产物落到 OSS/COS（S3 兼容），下载走预签名 URL，过期自动清理 |
| 长期记忆 | 任务结束后自动抽取用户偏好与关键结论，下次提问时语义召回（pgvector 向量检索） |
| 短期记忆 | 同一会话内多轮追问共享上下文，跨进程持久化 |
| SQL 安全 | 业务 MySQL 只读代理：语句类型白名单 + 表名白名单 + 查询超时 + 结果行数上限 |
| 文件治理 | 大小限制、扩展名 + MIME 双重校验、OOXML 格式兜底识别 |
| 审计日志 | 登录、任务增删、文件上传下载、SQL 执行等关键操作留痕 |
| 前端体验 | 会话式界面 + 侧边任务历史、执行轨迹、文件预览、报告在线编辑、多任务结果对比 |

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│  浏览器  React 19 + antd + Vite                              │
└───────────────┬─────────────────────────┬───────────────────┘
                │ HTTP (JWT)              │ WebSocket
                ▼                         ▼
┌─────────────────────────────────────────────────────────────┐
│  FastAPI (app/api/server.py)         【API 进程】            │
│   · 鉴权与任务归属校验      · 文件上传/下载/预览             │
│   · 任务入队                · WS 握手 + 历史回放             │
│   · 订阅 Redis 事件通道并转发给浏览器                        │
└──────┬──────────────────────────────┬───────────────────────┘
       │ 入队                          │ 订阅 events:{task_id}
       ▼                               │
┌──────────────────┐                   │
│  Redis           │◄──────────────────┘
│  队列 + 事件总线  │
└────────┬─────────┘
         │ 消费任务
         ▼
┌─────────────────────────────────────────────────────────────┐
│  arq Worker (app/queue/worker.py)    【Worker 进程】         │
│   · 执行 DeepAgents  · 状态全程落库  · 轮询取消标志          │
│   · 产物归档到对象存储  · 抽取长期记忆  · 定时清理过期文件    │
└──────┬───────────────┬──────────────┬───────────────────────┘
       │               │              │
       ▼               ▼              ▼
┌────────────┐  ┌────────────┐  ┌──────────────┐  ┌──────────┐
│ PostgreSQL │  │ 业务 MySQL │  │ 对象存储 OSS │  │ 本地知识库│
│ 应用表/记忆 │  │  (只读)    │  │  附件/产物   │  │  向量检索 │
│ 事件/审计   │  │            │  │              │  │          │
└────────────┘  └────────────┘  └──────────────┘  └──────────┘
```

**两个进程，一份状态。** API 与 worker 通过 PostgreSQL 和 Redis 通信，不共享内存，
因此两者可以独立重启、也可以分开部署到不同机器。

---

## 技术栈

**后端**

- Python 3.12 · FastAPI · uvicorn
- DeepAgents / LangChain / LangGraph（Agent 编排与记忆）
- PostgreSQL 18 + pgvector（应用表、短期记忆 `PostgresSaver`、长期记忆 `PostgresStore`）
- Redis + arq（任务队列、事件发布订阅）
- SQLAlchemy 2.0 async + Alembic（ORM 与迁移）
- boto3（S3 兼容对象存储）
- JWT + bcrypt（鉴权）

**前端**

- React 19 · TypeScript · Vite
- antd 5 · Tailwind CSS 4
- react-markdown + remark-gfm（报告渲染）

---

## 目录结构

```
多Agent电商系统/
├── main.py                     # 项目入口（uvicorn 启动 FastAPI）
├── alembic.ini                 # Alembic 配置（DSN 从 app.config 读，不写在这里）
├── requirements.txt
├── .env                        # 配置（不纳入版本管理，含密钥）
│
├── app/
│   ├── config.py               # 集中配置，所有子系统从这里读常量
│   ├── agent/
│   │   ├── main_agent.py       # 主智能体组装 + 会话工作区准备 + 任务执行
│   │   ├── prompts.py          # 加载 app/prompt/prompts.yml
│   │   ├── llm.py              # 模型构造
│   │   ├── memory_consolidator.py  # 任务结束后抽取长期记忆
│   │   └── subagent/           # 三个子智能体（网络/数据库/本地知识库）
│   ├── prompt/prompts.yml      # 提示词与子智能体路由描述（改这里即可调行为）
│   ├── api/
│   │   ├── server.py           # FastAPI 应用、WS 端点、路由挂载
│   │   ├── monitor.py          # 事件上报（落库 + Redis 广播）
│   │   ├── context.py          # user_id / session 的 ContextVar
│   │   ├── schemas.py          # 请求/响应模型
│   │   └── routes/             # tasks / files / memory / admin
│   ├── auth/                   # 密码哈希、JWT、依赖注入、登录注册路由
│   ├── db/                     # 引擎、会话、模型、Base
│   ├── queue/                  # arq worker、入队、队列设置
│   ├── services/               # 审计、事件存储、文件治理、记忆、SQL 防护、归属校验
│   ├── storage/                # 对象存储抽象与 S3 实现
│   ├── tools/                  # Agent 可调用的工具（搜索/数据库/知识库/PDF/Markdown）
│   ├── knowledge/              # 本地知识库向量检索
│   └── utils/                  # 事件循环、异步桥接、路径处理、Word 转换
│
├── migrations/                 # Alembic 迁移
├── scripts/init_db.py          # 一次性数据库初始化（幂等）
├── knowledge_base/             # 本地知识库语料（按行业分目录）
├── output/  updated/           # Agent 运行期临时工作区（非持久存储）
│
└── frontend/
    ├── src/
    │   ├── App.tsx             # 应用外壳与路由
    │   ├── components/         # 会话、任务历史、文件预览、报告编辑、结果对比等
    │   ├── hooks/              # useAuth、useDeepAgentSession
    │   ├── lib/                # api / auth / config / thread / turns
    │   └── types.ts
    └── package.json
```

---

## 快速开始

### 1. 前置依赖

下表「本项目实测版本」一列是开发环境实际跑通的版本（见 `requirements.txt`）。

| 组件 | 本项目实测版本 | 说明 |
|---|---|---|
| Python | **3.12.11**（conda 环境） | 依赖装在该环境中，请勿用系统默认 Python |
| Node.js | 24.16.0 | Vite 7 要求 `^20.19.0 \|\| >=22.12.0` |
| PostgreSQL | **18.6** + **pgvector 0.8.6** | 应用表 + 记忆 + 事件 + 审计，必须装 pgvector |
| Redis | 本地实例 | 任务队列 + 事件总线 |
| 对象存储 | 阿里云 OSS（S3 兼容） | 附件与产物持久层，COS/MinIO 同样适用 |
| MySQL | 8.0 | 业务数据库，**建议使用只读账号** |
| 本地知识库 | 语料目录 | `knowledge_base/`，按行业分目录 |

### 2. 安装依赖

```bash
# 后端
pip install -r requirements.txt

# 前端
cd frontend
pnpm install
```

### 3. 配置 .env

在项目根目录创建 `.env`，最少需填写以下内容（完整可选项见下一节）：

```ini
# ---- LLM ----
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_API_KEY=sk-xxxxxxxx
LLM_QWEN_MAX=qwen-max
LLM_QWEN_EMBEDDING=text-embedding-v3

# ---- 网络搜索 ----
TAVILY_API_KEY=tvly-xxxxxxxx

# ---- PostgreSQL ----
POSTGRES_USER=deepagents_user
POSTGRES_PASSWORD=你的密码
POSTGRES_DB=deepagents
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

# ---- 业务 MySQL（只读账号）----
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=readonly_user
MYSQL_PASSWORD=你的密码
MYSQL_DATABASE=your_business_db

# ---- Redis ----
REDIS_URL=redis://localhost:6379/0

# ---- 对象存储 ----
STORAGE_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
STORAGE_BUCKET=your-bucket
STORAGE_ACCESS_KEY=your-access-key
STORAGE_SECRET_KEY=your-secret-key
STORAGE_REGION=cn-hangzhou

# ---- JWT（生产环境务必更换）----
JWT_SECRET=请改成一段足够长的随机字符串
```

### 4. 初始化数据库

```bash
python scripts/init_db.py                       # 建扩展、跑迁移、建记忆表
python scripts/init_db.py --seed-allowlist      # 额外从业务 MySQL 播种 SQL 白名单
python scripts/init_db.py --admin zhang 123456  # 额外创建管理员账号
```

脚本是**幂等**的，重复执行安全。若 `CREATE EXTENSION vector` 因权限不足失败，
按提示把那条 SQL 交给 DBA 执行后重跑即可。

### 5. 启动服务

三个终端分别执行（均在项目根目录）：

```bash
# ① API 服务
python main.py
# 或：uvicorn main:app --reload --host 0.0.0.0 --port 8000

# ② Worker 进程（必需，否则任务只会排队不执行）
arq app.queue.worker.WorkerSettings

# ③ 前端开发服务器
cd frontend && pnpm dev        # http://localhost:5173
```

浏览器打开 <http://localhost:5173>，用初始化时创建的管理员账号登录。
健康检查：`GET http://localhost:8000/api/health`。

---

## 配置项说明

所有配置集中在 [`app/config.py`](app/config.py)，从 `.env` 读取。以下为可调项及其默认值。

### 数据库连接池

| 变量 | 默认 | 说明 |
|---|---|---|
| `PG_DSN` / `PG_SYNC_DSN` / `PG_MIGRATION_DSN` | 由 `POSTGRES_*` 拼装 | 分别供 asyncpg / langgraph / Alembic 使用，通常无需手填 |
| `DB_POOL_SIZE` | 10 | 连接池大小 |
| `DB_MAX_OVERFLOW` | 20 | 溢出连接数 |
| `DB_POOL_RECYCLE` | 1800 | 连接回收秒数 |
| `DB_ECHO` | false | 是否打印 SQL（调试用） |

### 任务队列

| 变量 | 默认 | 说明 |
|---|---|---|
| `TASK_MAX_ATTEMPTS` | 3 | 单任务重试上限 |
| `TASK_JOB_TIMEOUT_SECONDS` | 900 | 单任务执行超时 |
| `TASK_MAX_JOBS` | 2 | worker 并发任务数 |
| `TASK_RETRY_BASE_SECONDS` | 15 | 退避基数，第 n 次重试等待 `base × 2^(n-1)` 秒 |
| `TASK_CANCEL_POLL_SECONDS` | 2.0 | 取消信号轮询间隔 |

### 文件治理

| 变量 | 默认 | 说明 |
|---|---|---|
| `FILE_MAX_SIZE_MB` | 50 | 单文件大小上限 |
| `FILE_ALLOWED_EXTS` | `.md,.txt,.pdf,.docx,.xlsx,.xls,.csv,.png,.jpg,.jpeg` | 扩展名白名单 |
| `FILE_ALLOWED_MIME_PREFIXES` | `text/,image/,application/pdf,…` | MIME 嗅探兜底 |
| `FILE_RETENTION_DAYS` | 30 | 生成产物保留天数 |
| `UPLOAD_RETENTION_DAYS` | 7 | 上传附件保留天数 |
| `STORAGE_PRESIGN_EXPIRE_SECONDS` | 600 | 预签名下载地址有效期 |
| `STORAGE_USE_PATH_STYLE` | false | MinIO 等需设为 true |

### SQL 安全

| 变量 | 默认 | 说明 |
|---|---|---|
| `SQL_QUERY_TIMEOUT_MS` | 5000 | 单条查询超时 |
| `SQL_MAX_ROWS` | 500 | 返回行数上限 |
| `SQL_ALLOWLIST_AUTO_SEED` | true | 白名单为空时自动从业务库播种 |
| `SQL_ALLOWLIST_CACHE_SECONDS` | 60 | 白名单进程内缓存时长 |

### 鉴权与长期记忆

| 变量 | 默认 | 说明 |
|---|---|---|
| `JWT_SECRET` | `please-change-me…` | **生产环境必须更换** |
| `JWT_EXPIRE_MINUTES` | 1440 | 令牌有效期（24 小时） |
| `ALLOW_SELF_REGISTER` | true | 是否开放自助注册 |
| `CORS_ORIGINS` | localhost:5173 等 | 允许的前端来源 |
| `EMBEDDING_DIMS` | 1024 | 向量维度，需与 embedding 模型一致 |
| `MEMORY_RECALL_LIMIT` | 5 | 单次召回记忆条数 |
| `MEMORY_CONSOLIDATE_ENABLED` | true | 是否开启任务后记忆巩固（会多调一次 LLM） |

### 前端环境变量

在 `frontend/.env.local` 中配置（可选）：

```ini
VITE_API_BASE_URL=http://localhost:8000
VITE_WS_BASE_URL=ws://localhost:8000
```

不填时默认连 `http://localhost:8000`，WS 地址由其协议自动推导。

---

## API 接口

除 `/api/health`、`/api/auth/register`、`/api/auth/login` 外，所有接口都需要
`Authorization: Bearer <token>`。

### 健康检查

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |

### 认证 `/api/auth`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 注册账号 |
| POST | `/api/auth/login` | 登录，返回 JWT |
| GET | `/api/auth/me` | 当前登录用户 |
| POST | `/api/auth/logout` | 退出登录 |

### 任务 `/api`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/task` | 创建并提交任务（带 `thread_id` 则为续聊） |
| POST | `/api/task/{thread_id}/cancel` | 取消任务 |
| GET | `/api/tasks` | 任务历史列表（分页、按状态/关键词筛选） |
| GET | `/api/task/{thread_id}` | 任务详情与最终结果 |
| GET | `/api/task/{thread_id}/events` | 事件回放（`after` 游标增量、`type` 筛选） |
| GET | `/api/tasks/compare` | 多条任务结果对比（2~5 条） |
| DELETE | `/api/task/{thread_id}` | 删除任务（须先取消） |

### 文件 `/api`

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload` | 上传文件 |
| GET | `/api/files` | 文件列表 |
| GET | `/api/files/stats` | 文件统计 |
| GET | `/api/file/{file_id}/download` | 获取预签名下载地址 |
| GET | `/api/file/{file_id}/preview` | 预览文件 |
| GET | `/api/file/{file_id}/content` | 读取可编辑正文 |
| PUT | `/api/file/{file_id}/content` | 保存编辑后的正文 |
| DELETE | `/api/file/{file_id}` | 删除文件 |

### 长期记忆 `/api`

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/memories` | 我的长期记忆 |
| DELETE | `/api/memories/{key}` | 删除一条长期记忆 |

### 管理端 `/api/admin`（需 admin 角色）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/audit` | 审计日志 |
| GET | `/api/admin/users` | 用户列表 |
| PATCH | `/api/admin/users/{user_id}` | 修改用户角色/状态 |
| GET | `/api/admin/sql-allowlist` | SQL 表白名单 |
| POST | `/api/admin/sql-allowlist` | 新增白名单表 |
| DELETE | `/api/admin/sql-allowlist/{table_name}` | 移除白名单表 |
| GET | `/api/admin/stats` | 系统概览 |

### WebSocket

```
ws://localhost:8000/ws/{thread_id}?token=<JWT>&after=<last_event_id>
```

握手时先校验 JWT 与任务归属，随后回放该会话的历史事件（从 `after` 之后），
再转入实时推送。客户端每 25 秒发送一次 `ping` 保活。

关闭码含义：

| 码 | 含义 |
|---|---|
| 4401 | 令牌无效或过期 |
| 4403 | 无权访问该会话 |
| 4404 | 会话不存在 |

---

## 安全设计

**身份与隔离**
- 密码 bcrypt 哈希存储；JWT 携带 `sub`（用户 ID）与 `role`，有效期默认 24 小时。
- `thread_id` 由**服务端**生成并登记归属，前端无法伪造 ID 读取他人会话。
- 所有按 `thread_id`/`file_id` 访问的接口统一经归属校验：员工只能访问自己的资源，
  管理员可访问全员。

**SQL 安全**（业务 MySQL 只读代理）
- 语句类型白名单：仅允许 `SELECT/SHOW/DESCRIBE/EXPLAIN`，拦截 DML/DDL 与多语句。
- 表名白名单：查询涉及的表必须已登记在白名单中。
- 查询超时 + 结果行数上限，避免拖垮业务库。
- 建议数据库账号本身也授予只读权限，形成双重保险。

**文件安全**
- 扩展名白名单 + MIME 嗅探双重校验；对 Office 文件额外做 OOXML 结构兜底识别，
  避免合法文件被误拒。
- 文件落对象存储，下载通过短时效预签名 URL，不暴露存储凭证。
- 上传附件与生成产物按保留天数自动清理。

**审计**
- 登录/登出、任务创建与取消、文件上传/下载/预览、SQL 执行、管理操作均写入
  `audit_log`（含操作人、资源、IP 与详情）。

---

## 常见问题

**任务一直停在「排队中」**
Worker 进程没启动。执行 `arq app.queue.worker.WorkerSettings` 起一个 worker。

**前端报「WebSocket 连接异常」**
检查后端是否在跑，以及 `frontend/.env.local` 中的 `VITE_API_BASE_URL` 是否指向
正确的后端地址。若浏览器控制台出现 4401/4403/4404 关闭码，分别对应令牌失效、
无权限、会话不存在。

**修改代码后 WebSocket 连不上、页面不刷新轨迹**
若同时存在多个 API 进程（例如手动起过一个又用 `--reload` 起了一个），浏览器可能连到
旧进程。`netstat -ano | findstr :8000` 确认 8000 端口只有一个监听者。

**上传的 PDF/Word 无法被解析**
确认 `python-magic-bin` 已安装（Windows 下的 MIME 嗅探依赖它）。

**长期记忆检索不到东西**
确认 `CREATE EXTENSION vector` 已执行、`EMBEDDING_DIMS` 与 embedding 模型输出维度
一致，且 `MEMORY_CONSOLIDATE_ENABLED` 未关闭。首次使用需要有任务成功结束并完成
记忆巩固。

**如何调整 Agent 的行为与路由**
改 [`app/prompt/prompts.yml`](app/prompt/prompts.yml) 即可，无需改代码 —— 主智能体
与三个子智能体的提示词、描述都在这里。改完重启 API 与 worker 生效。

---

## 开发提示

- **Python 解释器**：本项目依赖装在 conda 环境（Python 3.12），请勿使用
  系统默认 Python。Windows 下建议加上 `PYTHONIOENCODING=utf-8` 以免中文日志乱码。
- **改了后端代码**：`python main.py` 带 `--reload` 会自动重启，**但 worker 不会**，
  需手动重启 `arq` 进程。
- **改了前端代码**：Vite 热更新自动生效；但涉及 WebSocket 或状态的改动，建议刷新
  页面并在浏览器里实际验证 —— 类型检查通不过浏览器层的问题（副作用依赖、状态机、
  渲染分支）。
- **数据库迁移**：改完 `app/db/models.py` 后执行
  `alembic revision --autogenerate -m "描述"` 生成迁移，再
  `alembic upgrade head` 应用。
- **管理员端页面暂时未进行开发**，仅提供 API 接口。