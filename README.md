# 传染病暴发调查与接触网络

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8303`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8303
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `case`：病例和调查状态；`contact`：接触者随访。
- `trajectory`：病例调查下的活动轨迹（地点、进入/离开时间）。时间缺失时以 `pending_time` 状态保存，必须填写 `pending_reason`，补全后用 `supplement_time` 动作转 `active`；时间改正用 `correct_time`，错误轨迹可用 `invalidate` 作废。
- `exposure`：同地点时段重叠自动推导出的暴露关系（系统维护，不可手工编辑）。

## 轨迹重叠与待随访

- 任意轨迹创建、补全、改正或作废后，系统重新计算所有 `active` 轨迹：同一地点（忽略首尾空格、大小写不敏感）且时间区间严格重叠的两条轨迹形成一条 `exposure`（端点相接不算重叠）。
- 时间改正后，不再成立的旧 `exposure` 置为 `withdrawn`，重新成立时恢复为 `active` 并刷新重叠时段；全过程写入审计（`derive` / `withdraw` / `recalculate`）。
- 暴露对方若是尚未登记为病例的人员，自动生成 `source=auto` 的 `contact`（按病例+人员去重）；关系撤回且接触者尚未开始随访时自动 `withdrawn`，关系恢复后自动 `reopen`。
- 已完成观察（`completed`）或本人已登记为病例的人员不出现在待随访名单中；自动接触者不影响手工建立的接触者记录。
- `GET /api/worklist` 返回重叠去重人数、有效暴露数、待补时间轨迹、待随访接触者和每条轨迹的重叠人数。演示页面（`/`）直接展示这些名单。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `GET /api/worklist`：重叠人数、待补时间轨迹和待随访接触者名单。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
