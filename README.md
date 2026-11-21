# kubernetes-admission-webhook-ordering-simulator

模拟多准入 Webhook 的顺序、变更冲突和失败回滚。

用 Python（兼容 3.8+）在本地模拟 Kubernetes 控制面的 Admission Webhook 执行链：
依次运行多个 Mutating Webhook（含 reinvocation），再运行 Validating Webhook，
逐次计算最终对象、对象版本与准入结果。

> **注意：本项目不连接任何 Kubernetes API Server。** Webhook 端点是进程内的
> Python 对象（`ScriptedEndpoint` / `FunctionEndpoint`），延迟是声明式的数值，
> 超时判断完全确定性，适合离线推演准入链行为与编写回归测试。

## 模拟的语义

- **调用顺序**：Mutating Webhook 按声明顺序串行执行，全部完成后按声明顺序执行
  Validating Webhook（真实集群中 Validating 是并行的，这里串行化以便确定性输出，
  所有拒绝原因会聚合）。
- **匹配规则**：每个 Webhook 可配置 `Rule`（operations / apiGroups / apiVersions /
  resources / scope）与 `namespace_selector`，不匹配则跳过并记录原因。
- **failurePolicy**：Webhook 报错或超时（声明延迟 > `timeout_seconds`）时，
  `Fail` 立即拒绝请求并中止链路；`Ignore` 记录后跳过该 Webhook 继续执行。
- **reinvocationPolicy**：第一轮中若有 Webhook 改写了对象，则排在其**之前**且
  策略为 `IfNeeded` 的 Mutating Webhook 会被重调一次（与 Kubernetes 一致，最多
  一轮）；重调若再次改写对象，链路不会无限循环，并输出提示。
- **对象版本**：对象版本从 1 开始，每次实际发生变更的补丁使其 +1；Validating
  Webhook 看到的是全部改写完成后的最终版本。
- **冲突检测**：某 Webhook 写入其他 Webhook 已写过的路径（含父子路径）时记录
  冲突，包含路径、双方名称与发生轮次。

## 输出内容

`AdmissionResult.summary()` 输出：

- 每次调用的顺序、阶段、轮次、耗时/超时、failurePolicy 处置；
- 每次变更的对象 diff（add/remove/replace + 路径 + 新旧值）与版本变化；
- reinvoke 原因（被哪些后续 Webhook 的改写触发）；
- 字段覆盖冲突记录；
- 最终拒绝依据（Validating 拒绝消息或 failurePolicy=Fail 的失败原因）。

## 使用

```bash
python3 main.py                 # 运行内置演示场景
python3 -m unittest discover -s tests -v   # 运行测试
```

最小示例：

```python
from admission_sim import AdmissionChain, Rule, ScriptedEndpoint, WebhookConfig
from admission_sim import runtime

hook = WebhookConfig(
    name="sidecar-injector", kind="mutating",
    rules=[Rule(operations=["CREATE"], resources=["pods"])],
    reinvocation_policy="IfNeeded", timeout_seconds=5,
    endpoint=ScriptedEndpoint([runtime.ok(patch=[
        {"op": "add", "path": "/spec/containers/-",
         "value": {"name": "envoy", "image": "envoy:1.30"}},
    ])]),
)
result = AdmissionChain([hook]).admit(pod_object)
print(result.summary())
```

## 测试覆盖

`tests/test_admission.py`（`python -m unittest`，15 个用例）：

- Pod sidecar 注入与注解注入、Validating 读取改写后对象；
- 字段覆盖冲突（last-write-wins + 冲突记录）；
- 超时 × failurePolicy（Fail 中止链路 / Ignore 跳过）；
- IfNeeded 重调一次、重调产生新冲突、Never 不重调、脚本化端点重复调用；
- Validating 拒绝依据聚合、规则与 namespaceSelector 匹配、对象版本递增。

## 目录结构

- `admission_sim/models.py` — Rule / WebhookConfig / WebhookResponse 数据模型
- `admission_sim/engine.py` — 执行链引擎（顺序、重调、超时、策略、冲突、版本）
- `admission_sim/patch.py` — RFC 6902 JSON Patch 子集（add/remove/replace/test）
- `admission_sim/diff.py` — 对象递归 diff
- `admission_sim/runtime.py` — 进程内模拟端点（脚本化 / 函数式）
- `main.py` — 演示场景；`tests/` — 单元测试
