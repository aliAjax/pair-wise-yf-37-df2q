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
- `movement`：病例调查下登记的活动轨迹（地点 + 进入/离开时间）。
- `exposure`：同地点时段重叠自动生成的暴露关系，仅由系统派生，不能手工创建。

## 活动轨迹与暴露关系

调查员把病例及同地点相关人员的轨迹登记为 `movement`：

- 必填 `case_id`、`location`；`enter_time`、`leave_time` 使用 ISO 8601（如 `2026-02-28T09:00`）。
- 时间不全时必须填写 `missing_time_reason`，轨迹进入 `pending_time`（待补时间）状态，不参与重叠计算；补全后执行 `supply_times` 转为 `recorded`。
- 同一地点、不同人员、时段严格重叠（半开区间，仅端点相接不算重叠）自动生成 `exposure`，记录双方轨迹与重叠时段。
- 已登记轨迹时间有误时执行 `correct_times`：该轨迹派生的旧暴露关系全部置为 `withdrawn`（审计可查），再按新时段重新计算。
- 接触者随访状态变化会反映在工作台：`contact` 完成观察（`completed`）或本人已成为有症状病例的人员不再进入待随访名单。

### 工作台接口

- `GET /api/worklist`：按病例汇总重叠人数，以及 `pending`（待随访）、`following`（随访中）、`excluded`（已完成观察/本人已发病）名单，并列出该病例下 `pending_times` 的待补轨迹。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

病例关联和时间窗口是调查辅助规则，不替代公共卫生部门的流行病学判断。
