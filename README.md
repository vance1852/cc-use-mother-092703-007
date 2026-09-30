# 科研成果许可与收益管理平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入，以及科研成果的协议版本、许可签发、续约、改进成果归属与分期收益分配。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险、成果转化和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/license_office/`：成果协议版本、权属依据、许可签发冲突管控、续约锁定、改进成果绑定、分期收益暂估/核定/结算、争议份额冻结、许可链重建与收入分配解释；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
PYTHONPATH=src python3 -m license_office.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入，以及成果协议版本化、冲突签发拦截、续约锁定、争议冻结和许可链重建流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m license_office.api --database license.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

## 成果许可与收益管理（license_office）

核心业务规则：

- **协议版本不可变**：权属份额、许可条件、承接方、改进成果和每期收益都通过外键绑定到签发或
  发生当时生效的协议版本；登记新版本会把旧版本标记为 `superseded`，但已绑定的授权、分成
  快照和已结算金额不变，新协议不回溯历史。
- **冲突签发拦截**：同一成果内，授权有效期与地域均相交时，排他/独家授权与任何授权冲突，
  同一承接方重复签发同样阻止（`POST /grants` 返回 409）。
- **续约锁定**：打开续约谈判（`POST /grants/{id}/renewals`）后，旧许可不得延期
  （`POST /grants/{id}/extend` 返回 409），也不得向该承接方另签新授权；续约签署在同一事务
  内按当前生效协议签发新授权并终止交接旧授权，放弃谈判后锁定解除。
- **分期收益**：`POST /revenue/periods` 暂估 → `.../assess` 核定（结算前可改，重算分成快照）→
  `.../settle` 结算；分成按协议权属比例计算，尾差归最大份额主体，总额分文不差。
- **争议只冻结相关份额**：`POST /disputes` 指定涉及的权属主体，仅这些主体的未结算金额冻结，
  其他主体份额照常结算；争议解决后解冻补齐。已结算部分任何情况下不被回冲，已结算收益期
  拒绝重新核定。
- **许可链与解释**：`GET /chains?achievement_id=` 或 `?licensee_id=` 重建协议版本、授权、
  改进成果和续约交接链；`GET /revenue/periods/{id}/explain` 逐笔说明收入适用的协议版本、
  权属依据、冻结/结算构成。

角色：`manager`（成果/承接方/授权/改进/续约）、`legal`（协议/授权/续约/争议）、
`finance`（收益/争议/解释）、`auditor`（只读审计、许可链与收入解释）。所有写接口需带
`X-Actor-Id` 头。
