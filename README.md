# 药物警戒案例处理系统

使用 Python 标准库实现的独立原型，覆盖多渠道案例接入、去重、随访更正、严重性医学裁定、分国家报告、逾期升级、跨区域权限、案例合并审计，以及安全信号处置（案例归并、信号建立、医学判定、措施登记与逾期提醒）。

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

- `POST /api/signals/scan`：按产品 + 事件词归并**有效案例**（`status != merged`；已合并来源只跟随目标案例，不重复计数），统计例数、严重数、死亡数和涉及区域。同一组合达到 **3 例**或**出现死亡**即建立信号；信号按归一化 (产品, 事件词) 唯一约束，重复检查只刷新证据，不产生第二份。
- `GET /api/signals`：信号列表，含例数/严重/死亡/区域、`overdue` 标记。
- `GET /api/signals/{id}`：信号详情，含证据案例、判定记录、处置措施和操作留痕。
- `POST /api/signals/{id}/decision`：医学审核员或全局管理员 `confirmed`/`rejected`，`rationale` 必填；同一信号只能判定一次。确认时可一并提交 `measure`、`owner`、`due_at` 登记首条措施。
- `POST /api/signals/{id}/actions`：已确认信号登记措施（措施、负责人、期限）。
- `POST /api/signal-actions/{id}/complete`：标记措施完成。
- `GET /api/signals/overdue`：存在逾期未完成措施的信号，页面顶部提醒。

权限：`reporter` 不能访问信号；`regional_lead` 只能看到本区域涉及的信号和证据（总数仍可见）；`medical_reviewer` 和 `global_admin` 可跨区处置。

## 分层

- `signal_rules.py`：判定规则（归并键、有效案例、3 例/死亡阈值），不依赖数据库和 HTTP。
- `signals.py`：信号数据层（信号、证据、判定、措施表及存取）。
- `app.py`：服务编排、权限和 HTTP 接口；`static/index.html`：页面操作（查看信号、证据、处置记录，判定、登记/完成措施，逾期提醒）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。信号阈值（3 例、死亡即触发）和产品/事件词归并规则为内置规则，接入受控词表和可配置阈值前需按业务扩展。
