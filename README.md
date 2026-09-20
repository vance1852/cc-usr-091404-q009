# CAPA 有效性判定

关联偏差根因、纠正预防措施、完成证据与后续观察指标, 帮助质量体系负责人判断措施是否**真正有效**——措施完成不等于有效, 只有观察窗口内数据达标才能关闭。

基于 Django REST Framework + SQLite。

## 核心规则

| 规则 | 实现 |
| --- | --- |
| 批准时固化判定计划 | `EffectivenessPlan.is_frozen`, 固化后 API 拒绝修改基线/目标/窗口/失败条件 |
| 完成 ≠ 有效 | 证据齐全仅使措施进入 `monitoring` 观察期 |
| 评审门槛 | 窗口内达到最小样本量且未触发失败条件, 判定才允许提交评审 |
| 数据不足 → 延长观察 | 窗口已过且样本不足时自动延长(`extended`), 绝不判为成功 |
| 新偏差自动重开 | 关联偏差后措施回到 `reopened`, 历史判定/签署全部保留 |
| 责任分离 | 责任人不能批准自己的计划与证据; 关闭须质量角色(Quality 组)签署 |
| 签署可撤回 | 撤回产生带理由的新版本, 原签署置为 `withdrawn` 并保留 |
| 全程审计 | 所有状态变更写入 `AuditLog`, 经 `/api/audit/` 查询 |

## 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser        # 或自建用户并加入 Quality 组
python manage.py runserver
```

质量角色 = 属于 `Quality` 用户组(或 is_staff)。组名可在 `settings.CAPA_QUALITY_GROUP` 配置。

## 定时评估

```bash
python manage.py evaluate_observations   # 建议 cron 每小时执行
```

扫描观察中/待评审/已重开的措施: 结论变化时生成新判定版本; 窗口已过且样本不足时自动延长观察期(默认 30 天, `settings.CAPA_EXTENSION_DAYS`)。

## 业务流程

```
草稿 → 待批准 → 已批准(计划固化) → 观察中 → 待评审 → 已关闭(有效/无效)
                                      ↑________ 新偏差重开 ________|
```

1. 建偏差 `POST /api/deviations/`、根因假设 `POST /api/hypotheses/`、措施 `POST /api/actions/`
2. 建判定计划 `POST /api/plans/`(观察窗口、最小样本量) + 指标 `POST /api/metrics/`(基线/目标/方向) + 失败条件 `POST /api/failure-conditions/`
3. `POST /api/actions/{id}/submit-plan/` → `POST /api/actions/{id}/approve/`(质量角色, 非责任人; 批准后计划固化)
4. 提交证据 `POST /api/evidence/` → 质量角色(非提交人)`POST /api/evidence/{id}/approve/`
5. `POST /api/actions/{id}/complete/` → 进入观察期(**完成不等于有效**)
6. 录数据 `POST /api/readings/`; 定时任务或 `POST /api/actions/{id}/evaluate/` 产生判定
7. 判定可评审后 `POST /api/actions/{id}/submit-for-review/`
8. 质量签署 `POST /api/actions/{id}/close/`(与系统判定不一致时必须填写说明)
9. 撤回 `POST /api/signoffs/{id}/withdraw/`(需理由, 生成新版本)

## 负责人看板

`GET /api/actions/{id}/dashboard/`(单项)与 `GET /api/actions/overview/`(全部)返回:

- **证据覆盖**: 总数/已批准/待审核/覆盖率
- **指标趋势摘要**: 窗口内样本数、均值、最新值、趋势(improving/worsening/stable)、是否达目标
- **剩余观察期**: 起止日期、剩余天数、当前样本量 vs 最小样本量、已延长次数
- **冲突意见**: 各版判定的同意/反对数及反对意见明细
- **可解释建议**: `recommendation.code` + 判定理由列表(如"窗口内样本量 2/3, 数据不足")

## 主要接口

| 路径 | 说明 |
| --- | --- |
| `/api/deviations/` | 偏差(创建时带 `linked_action` 即触发重开) |
| `/api/actions/` | CAPA 措施及上述各动作端点 |
| `/api/plans/` `/api/metrics/` `/api/failure-conditions/` | 判定计划(固化后只读) |
| `/api/evidence/` | 证据提交与审核 |
| `/api/readings/` | 观察数据 |
| `/api/evaluations/` | 判定历史(只读, `POST {id}/opinions/` 发表意见) |
| `/api/signoffs/` | 签署历史(只读, `POST {id}/withdraw/` 撤回) |
| `/api/audit/` | 审计日志, 支持 `entity_type`/`entity_id`/`actor`/`action` 过滤 |

## 测试

```bash
python manage.py test capa
```

33 个用例覆盖: 完整生命周期、计划固化、责任分离、完成≠有效、样本量门槛、失败条件、观察期延长、定时任务幂等、新偏差重开且历史保留、签署撤回版本化、审计与看板。
