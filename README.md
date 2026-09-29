# 科研成果许可与收益管理平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入；并提供高校/企业共有科研成果的权属协议版本、许可签发冲突检测、续约谈判、改进成果归属与分期收益（暂估→核定→争议冻结→结算）管理。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险、技术转移与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/license_revenue/`：成果权属依据、协议版本、许可链、改进归属与分期收益；
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
PYTHONPATH=src python3 -m license_revenue.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入以及成果协议版本、许可冲突、续约和分期收益流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m license_revenue.api --database license.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`（无需鉴权头），其余 JSON 接口使用 `X-Actor-Id` 头标识操作人。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

## 成果许可与收益服务（license_revenue）

围绕"协议版本不可变、一切绑定到生效版本"组织：

- **协议版本**：`POST /agreements` 起草（权属比例之和必须为 100%），`POST /agreements/{id}/versions/{n}/ratify` 由法务生效；新版本生效只把旧版本标记为 `superseded`，旧条款原样保留。
- **许可签发**：`POST /licenses` 必须指定已生效的 `agreement_id`/`version_no`。签发时把授权展开为 时间×地域（`CN:BJ` 从属于 `CN`，`WORLD` 覆盖全部）×领域 网格；独占/排他与相交范围冲突即返回 409 及冲突明细，同承接方重叠也必须走显式续约。
- **续约**：`POST /licenses/{id}/renewals` 进入谈判后，旧许可不能延期、不能终止；`POST /renewals/{id}/complete` 用新协议版本签发新许可并更替旧许可，`.../abandon` 放弃后旧许可才可 `extend`。
- **改进成果**：`POST /improvements` 绑定到母许可及其协议版本，按许可政策确定归属（许可方/承接方/共有/逐案）。
- **分期收益**：`POST /revenue/estimate` 暂估、`.../assess` 核定、`.../disputes` 只冻结争议收款方份额、`.../settle` 结算。核定复算时已结算金额锁定，毛收入不得低于已结算合计（不得倒改）；分成不足 100% 的差额记为机构留存。
- **重建与解释**：`GET /chains?achievement_id=` 或 `?licensee_id=` 重建 协议版本→许可→续约/改进/收益 链；`GET /revenue/installments/{id}/explain` 给出每笔收入逐行的协议版本、分成比例、计算公式与审计时间线。
