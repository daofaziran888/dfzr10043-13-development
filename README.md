# 织流工坊 · 网页流程自动化与RPA采集执行平台

面向RPA流程模板编排、元素定位漂移检测、机器人节点调度、执行步骤轨迹、凭据轮换、文件归集、采集批次确认与下游交接的流程自动化采集执行平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流程模板 | `flow_template` | 流程模板 | 模板编号、流程名称、起始地址 |
| 元素定位 | `element_locator` | 定位策略 | 定位编号、所属模板、元素名称 |
| 机器人节点 | `robot_node` | 执行节点 | 节点编号、节点名称、运行环境 |
| 流程执行 | `flow_run` | 执行实例 | 执行编号、关联模板、执行节点 |
| 步骤轨迹 | `step_trace` | 步骤轨迹 | 轨迹编号、关联执行、步骤序号 |
| 任务队列 | `task_queue` | 排队任务 | 队列编号、关联模板、优先级 |
| 凭据保管 | `credential_vault` | 登录凭据 | 凭据编号、适用站点、账号标识 |
| 表单字段 | `form_field` | 填充字段 | 字段编号、关联模板、表单名称 |
| 文件归集 | `file_drop` | 归集文件 | 归集编号、关联执行、文件名称 |
| 识别区域 | `ocr_region` | 识别区域 | 区域编号、关联模板、区域名称 |
| 排程计划 | `schedule_plan` | 排程计划 | 计划编号、关联模板、触发周期 |
| 异常规则 | `exception_rule` | 处置规则 | 规则编号、匹配特征、判定条件 |
| 重试策略 | `retry_policy` | 重试策略 | 策略编号、关联规则、最大次数 |
| 采集批次 | `capture_batch` | 采集批次 | 批次编号、关联执行、记录行数 |
| 字段映射 | `field_mapping` | 映射规则 | 映射编号、来源字段、目标字段 |
| 数据落库 | `data_landing` | 落库记录 | 落库编号、关联批次、目标存储 |
| 审计轨迹 | `audit_trail` | 审计条目 | 条目编号、操作对象、操作类型 |
| 资源额度 | `resource_pool` | 资源额度 | 额度编号、资源类型、总额度 |
| 版本发布 | `version_release` | 发布版本 | 发布编号、关联模板、版本号 |
| 运行指标 | `metric_board` | 指标读数 | 读数编号、统计维度、统计窗口 |
| 抽检样本 | `sample_check` | 抽检样本 | 样本编号、关联批次、抽样比例 |
| 交接包 | `handoff_pack` | 交接包 | 交接编号、关联批次、目标系统 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 归集证据与交接验件（app.evidence）

列表 CSV 只能给出清单索引，无法凭下载清单复核某个采集批次的原始字节。
`backend/app/evidence` 是一套纯标准库、磁盘内容寻址的证据子系统，让交接包
按实际归集内容验件，挂载在 `/api/evidence/*`：

| 文件 | 职责 |
| --- | --- |
| `app/evidence/store.py` | 内容寻址原件仓：sha256 为主键，只新增不覆盖；catalog 间接层支持「复制→复核→换定位→删旧」的安全迁移，以及丢失/按原摘要恢复 |
| `app/evidence/registry.py` | 归集登记：来源编号 `执行#序号` 唯一不复用；同名更正只追加，旧条目转 superseded 且原字节保留 |
| `app/evidence/export.py` | 证据导出：按执行导出来源编号、执行编号、字节大小、sha256、版本状态的证据 CSV，行级回调可覆盖导出途中迁移 |
| `app/evidence/transfer.py` | 断点续传下载：会话固定证据快照与签收人，按来源编号取件、offset 续传、前缀复核；中断可续，更正件无法替换在途内容，重复核验不重复计缺件 |
| `app/evidence/handoff.py` | 交接验件：读入证据件按来源定位原件并复核摘要，分别报告 `ok / missing / mismatch`，缺件给出待恢复的原归集编号 |
| `app/routers/evidence.py` | 12 个 HTTP 接口：collect、correct、export、migrate、mark-lost、restore、state、downloads、verify 等 |

关键保证：

- **来源可追溯**：证据每行带 `source_id`、`run_id`、`sha256`，指出内容来自哪次归集。
- **更正不毁原件**：补报同名更正件产生新来源编号；旧版本仍被旧任务/旧证据引用，
  原字节在内容寻址仓中不会被替换或删除。
- **迁移不影响验件**：物理路径迁移只改 catalog 定位，来源编号与摘要不变；
  导出/下载按现势 catalog 取件，真正物理丢失才报缺件。
- **在途不可替换**：下载项固定 `(source_id, sha256)`，按来源编号取该条目自己的
  摘要，另一次执行的同名件、同名更正件都无法顶替。
- **可恢复且不重计**：中断后签收人凭原证据续验；阻塞件在原件修复后重新判定，
  同一来源编号在重复核验中只计一条缺件；恢复字节与原摘要不符一律拒绝（422）。

运行期数据默认落在 `backend/var/evidence/`（已在 `.gitignore` 忽略），
测试通过临时目录隔离。测试见 `backend/tests/test_evidence_handoff.py`
（领域场景 16 例）与 `test_evidence_api.py`（路由冒烟 6 例）。

领域：数据工程
子方向：数据采集、爬虫与RPA
