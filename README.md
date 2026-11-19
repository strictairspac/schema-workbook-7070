# 实验委托模型拆解册

南麓材料实验室（9人）把委托单上的业务依赖整理为可验证的3NF关系，并保留候选键、闭包、最小覆盖和无损校验证据。

## 业务工作流

原先设计员在Excel里堆叠委托号、客户、检测项、方法与结果，再手工画表结构。痛点：依赖口径难确认、复合键容易漏判、拆表后无法证明无损。上游为委托单和已确认业务规则，本项目承接关系草案登记、函数依赖编辑、3NF合成、验证报告导出，下游为实验数据录入系统的数据库设计人员。

核心实体：关系草案 Relation、函数依赖 Dependency、分解报告 Report。4业务页和独立登录页；管理员另有审计页。业务规则不执行为SQL，属性名是纯数据。

## 快速启动

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 http://localhost:18243 ，后端 http://localhost:19243/docs ，MySQL localhost:17743。

|角色|账号|密码|权限|
|---|---|---|---|
|admin|admin@nanlu.test|Demo2026!|草案/依赖写入、运行、审计|
|operator|operator@nanlu.test|Demo2026!|草案/依赖写入、运行、导出|
|viewer|viewer@nanlu.test|Demo2026!|只读与导出，写API403|

## 一天的工作剧本

09:00 operator登录；09:05打开 `NL-1117-A` 材料检测委托单；09:10查看五属性 `委托号,客户,检测项,方法,结果`；09:15确认 `委托号→客户`、`检测项→方法`、`委托号,检测项→结果`；09:20执行 `shift-20261117` 分解任务；09:25候选键为 `{委托号,检测项}`，拆出委托客户、检测项方法、委托检测结果3个关系；09:30导出 `report-1.json`；09:40 viewer复核3NF/依赖保持/无损均通过；10:00 admin审计。

另有 `NL-1117-B` 的循环依赖 A→B/B→A与独立属性C，候选键AC/BC；`NL-1117-C` 没有确认依赖，完整属性集作为唯一候选键，不凭空猜测业务规则。

## 算法说明

Python真算，MySQL保存草案/依赖与结果，Redis传递任务，独立worker执行。

1. 属性闭包：迭代应用所有X→a，直到不再增加属性。
2. 候选键：先取不出现在RHS的必需属性，再按大小枚举其余属性子集；若闭包覆盖全集且不含已知候选键，加入结果。**最多10个属性/64条依赖**，最多1024个子集；超限API422/算法ValueError，避免指数搜索失控。
3. 最小覆盖：RHS拆成单属性，去平凡依赖；逐一删除左侧多余属性、移除冗余依赖。
4. 3NF合成：按最小覆盖左侧分组，删除被包含关系；若无关系含候选键，补入候选键关系。无依赖时保留全属性关系。
5. 验证：精确生成F+在各分解关系上的投影，枚举局部候选键，逐非平凡投影依赖检查3NF；所有原始依赖须可由投影并集推出。tableau追赶法依据FD合并符号，出现全a符号行则无损，报告包含见证行、合并步骤与最终表。

## 受限 3NF 分表求解（表数 / 每表属性双上限）

「受限分表」页在给定**表数上限 K 与每表属性数上限 M** 下，寻找满足三范式、可无损拼回、并保持全部函数依赖的分表；目标字典序为：表数最少，表数相同时重复存储属性（Σ|R_i|−|U|）最少。代码集中在 `backend/src/solver/`（纯标准库、位集表示、零外部依赖）。

1. 候选表：枚举容量内全部属性子集，逐一用精确投影依赖验证 3NF，只保留「承载某条最小覆盖依赖」或「含某个候选键」的子集，其余不可能进入最优解。
2. 分层穷举：按表数 k=1..K 逐层 DFS（候选下标严格递增），叶子统一校验覆盖全集、含键表、依赖保持与 tableau 无损。仅保留可证明安全的剪枝（容量上界、键表必要性、可接受重复量下界）；**不**把「最小覆盖依赖必须同表」作为门槛——依赖保持允许经多表投影间接推导，否则会漏判可行解。
3. 时限与检查点：每个工作单元携带可序列化的 DFS 前沿栈。到时把未展开前沿拆成新单元落库，状态置 `waiting`（待续跑），保留已验证最好方案；续跑沿用原草案与上限，已排除组合不重复执行。只有把必要层级全部搜遍才报 `optimal`（已最优）或 `infeasible`（无解）。
4. 并发与幂等：单元以 `(task_id, unit_key)` 唯一、行锁原子认领、租约过期可接管；事件以 `event_key` 唯一，重试/并发不重复发布；全局最优在同一事务按（表数, 重复量）合并。迟到的旧租约完成会被认领者校验拒绝。
5. 状态机：`queued / running / waiting（待续跑）/ optimal（已最优）/ infeasible（无解）/ failed`，前端徽章与详情页明确区分后三态。队列优先 Redis，不可用时熔断降级为进程内队列；CLI（`python -m src.solver.cli`）支持无数据库的离线求解与状态文件续跑。

正确性由独立暴力枚举交叉验证：随机小实例上枚举全部子集族逐一校验 3NF/依赖保持/无损，与求解器结论比对（`backend/tests/`，120+ 随机实例零不一致）。

复合、循环、空决定项、无依赖均支持。不在任何RHS中的属性强制进入候选键。输入入队时冻结快照，后续修改不会改变已有报告。结果JSON含SHA256，固定输入应有固定输出。失败态包括引用关系外属性、重复/空属性、属性超过10、依赖超过64、队列错误；页面可恢复并重新执行。

这是小型关系模型设计辅助工具，FD由人工提供，不能从脏数据自动推断业务规则；不连接生产数据或自动执行DDL。精确投影在10属性内有界，仍可能随依赖复杂度增加耗时。单worker串行队列，不带生产任务重试/崩溃续跑。

## 工程结构

|服务|技术|职责|
|---|---|---|
|frontend|原生HTML/CSS/JS + Nginx|4业务页、登录、共享业务表格与报告组件|
|backend|Python3.11/FastAPI/SQLAlchemy/PyJWT|校验、权限、审计、快照与导出|
|worker|Python3.11|闭包、候选键、3NF与chase|
|db|MySQL8.0|JSON属性与依赖、任务报告|
|redis|Redis7|持久任务队列|

目录：`frontend/src/{api,stores,types,components/common,hooks,pages,router,utils,constants}`；`backend/src/{routes,controllers,services,models,schemas,middlewares,utils,constants,config,solver}`；`backend/scripts/seed_demo.py`；`backend/migrations/`。每核心实体贯穿模型/schema/service/route/API/store/page。组件table/badge/frame复用，hooks/session/feedback和utils/forms/html共用；request_id/errors双后端中间件。受限求解器 `backend/src/solver/{theory,search,orchestrator,runner,memory_store,search_store,cli}`，测试在 `backend/tests/`。

枚举：角色admin/operator/viewer、经典分解任务queued/running/completed/failed；受限分表任务queued/running/waiting/optimal/infeasible/failed（待续跑/已最优/无解在前端徽章与详情页区分），工作单元pending/running/done/checkpointed。后端定义位于 `backend/src/constants/enums.py` 与 `backend/src/solver/solver_states.py`，前端位于 `frontend/src/constants/enums.js`；security/reports、frame/badge消费；worker持久化状态。

环境变量见.env.example：COMPOSE_PROJECT_NAME、FRONTEND_PORT/BACKEND_PORT/DB_PORT、DB_PASSWORD、JWT_SECRET、SEED_DEMO。默认密码仅本地演示，JWT8小时。seed按自然键只补缺失关系及初始依赖，不覆盖用户修改。schema由SQLAlchemy metadata.create_all初始化，无在线迁移引擎。

本地开发：Python3.11安装backend/requirements.txt，配置DATABASE_URL/REDIS_URL后从backend运行 `uvicorn src.main:app --reload`、`python -m src.worker`；前端用Nginx同源代理/api。5服务均有健康检查和健康依赖，数据库/Redis/worker产物为独立命名卷。停止清空：`docker compose down -v --remove-orphans`。

UI为酒红档案册，双字体由Google Fonts加载，离线有回退且业务不依赖字体网络。

## License

MIT。真实测试脚本、截图与结果保存在源码外 `../../test-artifacts/gb-4104/`。
