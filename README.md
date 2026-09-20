# 偏差与 CAPA 有效性判定系统

面向质量体系负责人的 Django REST Framework 应用：把**偏差根因假设 → 纠正/预防措施 →
完成证据 → 观察指标 → 有效性判定 → 质量签署 → 后续偏差复发**串成闭环，避免
“纠正措施按期完成 ≠ 措施真正有效”导致同类灌装量偏差反复进入生产。

## 核心规则

1. **基线在措施批准时固化**：观察窗口天数、最小样本量、指标基线值/目标值、
   失败条件（聚合越阈或单点越限次数）在批准时一次性锁定，之后只读。
2. **措施完成 ≠ 有效**：完成措施（须有非责任人批准的证据）只打开观察窗口。
3. **提交评审门槛**：窗口内每项指标达到最小样本量（支持按点加权）**且**
   未触发任何失败条件，状态才为 `ready`，才能提交评审；窗口未结束但样本
   已达标允许提前评审。
4. **数据不足只延长，不判成功**：窗口结束样本不足时自动延长窗口（次数可配），
   达到上限仍不足进入 `data_insufficient`，由质量角色裁决，系统绝不判有效。
5. **失败条件优先**：聚合值越过失败阈值、或单点越限超过允许次数，立即判
   `ineffective`，样本再足也不能提交/批准。
6. **相关偏差自动重开**：关联新发生的同类偏差后，判定自动生成新版本重新观察；
   历史版本与其结论永久保留（状态置为 `reopened`，`is_current=false`），不可删除。
7. **责任分离**：责任人不能批准自己的措施基线、完成证据，不能自行确认完成。
8. **质量签署关闭**：只有质量角色（超管或 `profile.role=quality`）可签署；
   撤回签署不删记录，而是生成一条带理由的 `withdrawn` 新版本，判定回到评审中。
9. **全程审计**：所有状态变更写 `AuditLog`，审计接口仅质量角色可查。

## 判定状态机

```
pending（措施未完成）
  └─完成(有已批准证据)→ observing
        ├─ 样本足且无失败 → ready → in_review（提交评审）
        │                         ├─ 质量 approve_close → effective（可关闭偏差）
        │                         ├─ 质量 reject        → ineffective
        │                         └─ 撤回签署(带理由)    → in_review（新版本签署）
        ├─ 窗口结束样本不足 → extended（自动延长）→ observing …
        │                     达延长上限 → data_insufficient（质量裁决）
        └─ 触发失败条件 → ineffective
相关偏差关联 → 归档旧版本(reopened) → 新版本 observing
```

## 技术栈

- Python 3.11 / Django 5.x / Django REST Framework 3.18
- SQLite（零外部依赖，`CAPA_DB_PATH` 可指定库路径）
- 定时逻辑：管理命令 `run_effectiveness_checks`，交由 cron / systemd timer 调度

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser          # 超管即具备质量角色
python manage.py runserver
```

创建带角色的用户（管理员，Basic/Session 认证）：

```bash
curl -u admin:xxx -X POST http://localhost:8000/api/users/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"qa","password":"pass1234","role":"quality"}'
curl -u admin:xxx -X POST http://localhost:8000/api/users/ \
  -H 'Content-Type: application/json' \
  -d '{"username":"owner","password":"pass1234","role":"owner"}'
```

### 典型流程（API）

| 步骤 | 接口 | 操作角色 |
|---|---|---|
| 登记偏差 | `POST /api/deviations/` | 任意 |
| 记录根因假设 | `POST /api/hypotheses/` | 调查人员 |
| 建措施并指定责任人 | `POST /api/actions/` | 任意 |
| 批准时固化基线+指标 | `POST /api/actions/{id}/freeze_baseline/` | 质量（非责任人） |
| 提交完成证据 | `POST /api/evidences/` | 责任人 |
| 批准证据 | `POST /api/evidences/{id}/review/` | 质量（非提交人） |
| 确认措施完成、开窗 | `POST /api/actions/{id}/complete/` | 质量（非责任人） |
| 录入观察数据 | `POST /api/observations/` | 任意 |
| 查有效性看板 | `GET /api/actions/{id}/assessment/` | 任意 |
| 手动触发评估 | `POST /api/actions/{id}/evaluate_now/` | 任意 |
| 提交评审 | `POST /api/actions/{id}/submit-review/` | 任意 |
| 记录冲突意见 | `GET/POST /api/actions/{id}/opinions/` | 任意 |
| 质量签署/退回 | `POST /api/actions/{id}/sign/` | 质量 |
| 撤回签署（需理由） | `POST /api/actions/{id}/withdraw-signoff/` | 质量 |
| 关联复发偏差并重开 | `POST /api/actions/{id}/link-related/` | 质量 |
| 关闭偏差 | `POST /api/deviations/{id}/close/` | 质量 |
| 查判定历史版本 | `GET /api/evaluations/?action=` | 任意 |
| 审计日志 | `GET /api/audit-logs/` | 质量 |

固化基线请求示例：

```json
{
  "window_days": 30,
  "min_sample_size": 20,
  "max_extensions": 3,
  "metrics": [{
    "name": "平均灌装量偏差(mL)",
    "direction": "decrease",
    "baseline_value": 8.0,
    "target_value": 2.0,
    "fail_condition": "aggregate_threshold",
    "fail_threshold": 3.0
  }, {
    "name": "单点越限次数",
    "fail_condition": "individual_breach",
    "fail_threshold": 3.0,
    "allowed_breaches": 1
  }]
}
```

### 看板返回内容（`assessment`）

- `engine`：系统状态、**可解释建议码与逐条理由**、剩余观察期（天）、各指标
  样本覆盖（实际/最小）、加权聚合值、越限次数、前后半窗口均值、斜率与
  “改善/恶化/持平”趋势；
- `evidence_coverage`：证据总数、已批准/拒绝/待批及明细；
- `conflict_opinions`：支持有效 / 质疑 / 中立的冲突意见；
- `signoffs`：签署链（含撤回版本与理由）；
- `related_deviations`：后续相关偏差；
- `evaluation_history`：所有判定版本（历史结论不删除）。

### 定时评估

```bash
# 建议每小时执行；窗口到期时自动延长、达标置 ready、失败置 ineffective
python manage.py run_effectiveness_checks
# 指定评估时刻（复盘/测试）
python manage.py run_effectiveness_checks --as-of=2026-10-15T09:00:00+08:00
```

crontab 示例：

```cron
7 * * * * cd /opt/capa && .venv/bin/python manage.py run_effectiveness_checks >> /var/log/capa-check.log 2>&1
```

## 测试

```bash
python manage.py test quality        # 40 个用例：引擎规则 / 服务工作流 / API / 定时命令
```

测试覆盖：责任分离各环节、基线只读、样本不足延长而非成功、失败条件阻断、
提交后新增失败数据导致签署被拒、撤回签署留痕、相关偏差重开且历史保留、
偏差关闭前置条件、审计接口权限。

## 代码结构

```
quality/
  models.py      领域模型（偏差/假设/措施/基线/指标/判定版本/证据/意见/签署/审计）
  engine.py      纯函数判定引擎（样本、失败条件、窗口、延长、趋势、建议）
  services.py    业务编排（事务、权限、审计、版本化重开）
  views.py       DRF 资源与动作接口（含 assessment 看板）
  roles.py       质量角色判定
  management/commands/run_effectiveness_checks.py  定时入口
  tests/         引擎、服务、API、命令测试
```
