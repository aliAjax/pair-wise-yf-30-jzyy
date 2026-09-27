# 药物警戒案例处理系统

使用 Python 标准库实现的独立原型，覆盖多渠道案例接入、去重、随访更正、严重性医学裁定、分国家报告、逾期升级、跨区域权限和案例合并审计。

## 运行

要求 Python 3.11+。

```bash
python3 app.py --db pharmacovigilance.db
```

默认监听 `127.0.0.1:8201`。首页为 `http://127.0.0.1:8201/`，健康检查为 `/health`。

所有接口使用请求头 `X-User-Id`、`X-Role` 和区域角色必需的 `X-Region`。角色为 `reporter`、`regional_lead`、`medical_reviewer`、`global_admin`。

## 主要接口

- `POST /api/cases`：录入案例，`dedupe_key` 相同则返回已存在案例。
- `GET /api/cases`、`GET /api/cases/{id}`：按权限查询。
- `POST /api/cases/{id}/followups`：用 `expected_revision` 防止覆盖随访。
- `POST /api/cases/{id}/medical-review`：医学审核员更新严重性、死亡和关联性。
- `POST /api/cases/{id}/reports`、`POST /api/reports/{id}/submit`：生成并提交分国家报告。
- `POST /api/cases/{id}/merge`：全局管理员合并重复案例。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。

## 安全信号处置

信号数据、判定规则和页面操作分开维护：数据在 `signals/signals_decisions/signal_actions` 表，
规则集中在 `signals.py` 的 `SignalRules`（默认：同产品+同事件词有效案例达到 3 例，或出现 1 例死亡即建立信号），
页面为 `static/signals.html`。

- 归并口径：按归一化后的产品名和事件词分组，只统计有效案例（`status != 'merged'`）。
  已合并来源案例跟随目标案例（intakes 合并时迁移），不重复计数。
- `POST /api/signals/scan`：医学审核员/全局管理员执行检查；重复扫描不产生第二份信号，只刷新统计。
- `GET /api/signals`、`GET /api/signals/{id}`：查看信号、证据案例和处置记录。
  区域负责人只看本区域涉及的信号及其证据，全局管理员和医学审核员可跨区。
- `POST /api/signals/{id}/decision`：医学审核员确认（confirmed）或驳回（rejected），必须填写依据，且只能判定一次。
- `POST /api/signals/{id}/actions`：确认后由区域负责人/全局管理员登记措施、负责人和期限；
  `POST /api/signals/{id}/actions/{aid}/complete` 标记完成（幂等）。
  期限按 UTC 当天 23:59:59 截止，逾期未完成的措施在列表和页面标红提醒。
- 页面入口：`/signals.html`。


## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。
