# FormuLab · 受约束的混合配方试验设计

从零实现的配方（混合物）试验设计系统：导入虚构历史配方与响应，在
「总量恒定 + 成分上下限 + 禁配 + 互斥组 + 加工档位」构成的可行区域上拟合
响应面，并用统一的 Bayesian D 准则推荐下一批试验。后端 FastAPI + SQLite，
前端零构建的原生 ES Module + 手写 SVG 图表，不依赖任何前端框架或构建工具。

## 快速开始

```bash
# 1) 依赖（Python 3.11+）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 启动（数据库默认落在 data/formulab.db，可用 FORMULAB_DB 指定其它路径）
.venv/bin/python -m uvicorn backend.app.api:app --host 127.0.0.1 --port 8137

# 3) 浏览器打开
#    http://127.0.0.1:8137/
```

首次打开若库为空，页面会提示「载入演示数据」，等价于：

```bash
curl -X POST http://127.0.0.1:8137/api/demo          # 幂等：已有研究则直接返回
curl -X POST "http://127.0.0.1:8137/api/demo?force=true"  # 清空并重建演示
```

演示数据包含 17 条虚构历史配方：4 条试件开裂 / 无响应（保留并标注为失败，不参与拟合），
2 条来自更宽上下限的旧产线（导入但判定区域外，不参与拟合），其余 11 条参与拟合；
另有 5 种原料的期初库存，以及一个已规划好第一批 12 次试验的设计任务。

运行测试（43 个用例，约 50 秒）：

```bash
.venv/bin/python -m pytest backend/tests -q
```

## 页面

| 页面 | 能做什么 |
| --- | --- |
| 概览 | 关键数字、下一批推荐（三元图 + 组成条）、原料占用、需要留意的诊断 |
| 试验区 | 编辑总量 / 网格份数 / 上下限 / 禁配对 / 互斥组 / 加工档位；保存后立即重算候选点；附「配方可行性试算」 |
| 设计任务 | 批量与档位参数、本批配方表格（投料 / 锁定 / 取消）、重新规划、版本历史 |
| 原料台账 | 余额 / 占用 / 可用、占用明细（可逐条取消释放）、只追加的台账流水、登记入库 |
| 拟合与辨识 | 本批前后可辨识参数数、先验归一化预测方差、参数后验 SD 图、响应散点、诊断说明、回归系数表 |
| 历史数据 | 历史配方、响应的导入（JSON）、失败标记与拟合状态 |

交互闭环：**确认投料**（扣减库存、不可再取消）→ **录入响应**（更新拟合与辨识）→
**重新规划**（用新数据 + 剩余库存推荐下一批）。**取消**只对未投料试验生效，
它释放占用；**锁定**的配方在重新规划时被原样保留。

## 方法要点

### 1. 可行区域：精确整数配比，绝不「先框后归一化」

区域写作单纯形的一个截面：`sum_i x_i = total`、`lower_i <= x_i <= upper_i`，
再叠加禁配对（两成分不能同时 > 0）与互斥组（组内至多一个 > 0）。
候选点（`backend/app/mixture.py`）只有两种来源：

- **格点**：枚举 `n_steps` 的整数组成，每个成分恰好是 `total / n_steps` 的整数倍，
  和恒等于 `total`（按构造精确，不经过任何归一化）；
- **顶点**：求解 `{sum x = total}` 与 `k-1` 个有效上下限超平面的交点，做秩检验，
  只保留满足全部约束的解——这些是约束多面体的真实顶点，用来补上格点够不到的角。

测试里用暴力枚举逐点核对了候选集合（`test_candidate_set_matches_brute_force`），
并显式验证了「不存在先选长方体再归一化的点」（`test_no_normalisation_of_box_corners`）。

### 2. 模型：参考编码的 Scheffé 形式

```
y = beta_ref + sum_{i != ref} (beta_i - beta_ref) z_i
    + sum_{(i,j) 可共存} gamma_ij z_i z_j + sum_l delta_l 1[level = l] + e
```

`z_i = x_i / total`。Scheffé 形式下线性项之和恒为 1，所有系数可以整体平移而
预测不变，因此至多 k-1 个线性方向可辨识；以基准成分作差写成对比，
秩亏就只可能来自设计本身，而不是代数上的平凡共线。禁配的对在区域内
`z_i z_j == 0`，因此交互项只对**可能共存**的成分对建列。

`identifiability` 一律用**未正则化**的秩判断；正则化只用于数值稳定。

### 3. 准则：一个可核对的目标函数

对整批设计 `D`（配方 + 档位一起算），信息矩阵 `M(D) = X'X`（列已按尺度标准化），

```
Phi(D) = log det(M(D) + ridge·I) - log det(ridge·I),   ridge = 1e-8
```

即各向同性先验 `N(0, 1/ridge)` 下的 Bayesian D 准则。**每一个候选（配方 × 档位）
与每一次交换都用同一个 Phi 求值**，所以「离散加工档位」与「连续配比」的取舍
不是两条经验规则，而是同一个标量下的比较（`test_process_tradeoff_uses_same_criterion`）。

批内选择是**联合**的，不是逐条贪心：信息是整批矩阵的联合行列式，重复配方对
行列式的边际增益会自然趋近 0（`test_batch_is_jointly_optimal_not_greedy_per_run`
用穷举证明了贪心不是最优）。

- 小问题（候选 × 档位 ≤ 26 且组合数 ≤ 60000）：**精确枚举**所有组合，返回全局最优，
  并在页面上标注 `exact`；
- 否则：贪心前向 + 最优改进的两两交换，所有步骤同样用 Phi 评判，标注 `greedy+exchange`；
- 档位覆盖下限（每档至少 `min_per_level` 次）作为硬约束在搜索内部执行。

`test_exact_search_matches_brute_force_optimum` 用独立暴力枚举核对精确搜索的最优值。

### 4. 满批不可行：返回可行数与阻碍约束

库存按「投料质量」计：每次试验占用 `usage_i = charge_mass · x_i / total`。
规划时以 `可用 = 台账余额 − 其他任务的占用` 为上限，在**联合批次**上一并检查。

- 若某原料先耗尽，规划返回**能凑出的最多试验数**（`feasible_size`）、
  `partial = true`，以及逐条的阻碍约束（`kind: stock`，含缺多少、需要多少、可用多少）；
- 若批量不足以覆盖全部档位，返回 `kind: level_coverage`；
- 若区域本身被禁配清空，返回 `kind: forbidden` / `empty_region` 及被剔除的点。

这些阻碍会原样出现在「概览 → 下一批」的红色面板和任务页里
（`test_stock_runs_out_and_partial_batch_reports_blocking`、`test_stock_shortfall_reports_blocking`）。

### 5. 库存台账：占用、投料、取消、多任务

`stock_entries` 是**只追加**的流水（入库 `receipt` / 库存调整 `adjustment` /
投料 `dose`），余额 = 流水求和。试验占用不入流水，而是由 `status='planned'`
的 `usage_json` 求和得到：

```
available = ledger_balance − sum(usage of planned runs)
available_for_task = ledger_balance − sum(usage of OTHER tasks' planned runs)
```

因此：

- **只占用，不扣减**：规划出 12 条试验不会改变余额，只减少「可用」；
- **确认投料**把计划行翻成 `dosed` 并在同一事务里写下负数流水——占用转为消耗，
  可用量不变（`test_dose_moves_hold_to_consumption`）；
- **取消**只允许 `planned` 行，不写流水，占用直接消失，可用量回升
  （`test_cancel_releases_only_unstarted_runs`）；已投料试验无法取消；
- **多个设计任务不能重复花同一份原料**：规划任务 B 时只把 A 的占用当作已占用，
  自己旧版本的占用则先释放再重算（`test_two_tasks_cannot_spend_the_same_stock`）；
- **并发确认**：写入用 `BEGIN IMMEDIATE` + 进程内 `threading.Lock`，
  两个线程同时确认投料时只有一个成功，另一个得到 409，库存永不为负
  （`test_concurrent_confirmation_cannot_overspend`）；
- **重新规划**会取消该任务上一版**未锁定**的试验，再生成新一版；锁定配方原样
  带入新版本（含不在格点上的自由配方），且不会重复占用
  （`test_locked_run_survives_replan`、`test_locked_run_is_reinserted_locked_not_duplicated`）。

### 6. 辨识度：追加前后可以比较

- **可辨识参数数**：追加本批前 / 后，模型矩阵的未正则化秩；
- **预测方差 / 先验**：对可行区域中每个候选点，计算
  `ridge · x'(X'X + ridge·I)^{-1} x / (x'x)`，即后验预测方差与先验预测方差之比，
  取值 `[0, 1]`，`1` 表示「数据对此处一无所知」。它不依赖设计的秩，
  所以不同样本量的设计可以直接比较（`test_predictive_variance_metric_is_prior_relative`）。
  演示数据：`10 → 14` 个可辨识参数，区域平均预测方差 `15.6% → 0.02%`。
- 仍不可辨识的方向会在诊断页列出并解释原因（演示中 `E` 受 `B×E` 禁配与上限 25 夹击，
  只能取 0 或 25，因此 `E − A`、`A×E`、`C×E`、`D×E` 在本区域无法单独辨识）。
- **共线设计**会被单独标记（`test_collinear_design_is_detected`）；
  失败试验保留在台账里、消耗原料、注明原因，但不进入拟合
  （`test_failed_run_is_excluded_from_fit`、`test_mark_failed_keeps_material_consumed`）。

## 目录

```
backend/app/
  mixture.py   区域校验、整数格点、真实顶点、消耗向量
  design.py    参考编码模型、D 准则、精确/启发式联合选批、拟合与秩
  db.py        SQLite 模式、只追加台账、事务与并发锁
  service.py   任务规划/改版、诊断、快照
  api.py       FastAPI 路由与静态文件
  seed.py      虚构历史数据与演示研究
backend/tests/ 43 个用例：混合区域、设计准则、台账与并发
frontend/      index.html + 原生 ES 模块视图 + 手写 SVG 图表
data/          SQLite（已 gitignore）
```

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/studies/{id}` | 研究配置、库存、任务、历史 |
| PUT | `/api/studies/{id}/config` | 保存试验区（库存差量记为「库存调整」流水） |
| GET | `/api/studies/{id}/region` | 候选点、统计、阻碍 |
| POST | `/api/studies/{id}/region/check` | 判断给定配方是否可行 |
| GET/POST | `/api/studies/{id}/inventory` `/receipt` | 台账与登记入库 |
| GET/POST | `/api/studies/{id}/history`、`/import` | 历史配方与响应 |
| GET/POST | `/api/studies/{id}/tasks` | 任务列表 / 新建（立即规划） |
| POST | `/api/studies/{id}/tasks/{tid}/plan` | 重新规划（新版本；保留锁定项） |
| POST | `/api/studies/{id}/runs/{rid}/lock` | 锁定 / 解锁 |
| POST | `/api/studies/{id}/runs/{rid}/dose` | 确认投料（扣减库存，不可撤销） |
| POST | `/api/studies/{id}/runs/{rid}/cancel` | 取消未投料试验（释放占用） |
| POST | `/api/studies/{id}/runs/{rid}/response` | 录入合成响应 |
| POST | `/api/studies/{id}/runs/{rid}/fail` | 标记失败（保留消耗，排除拟合） |
| GET | `/api/studies/{id}/diagnostics` | 追加前后辨识度、预测方差、系数、诊断 |
| GET | `/api/studies/{id}/report` | 完整报告快照（便于导出） |
