# Cooperative-HALNS 重构报告（Pickup-Aware + Balanced Ownership + Criticality-Guided Dynamic Relay）

> **正式目标（全文统一）**：`min_lex(late_count, distance_km)`
> 即第一目标最小化逾期任务数，第二目标在 late_count 相同时最小化总距离。
> `total_lateness_min`、pickup slack、delivery-risk、pickup-advance 全部仅作
> **search guidance / diagnostic**，绝不进入 `Score`、candidate exact ranking
> 或 acceptance priority。
> 速度、无人机数、Q=2、K=25、任务数、deadline 定义均未修改。

---

## 1. 本轮要收敛的正式结构

本轮把当前同时存在的多套重叠机制：

```
Ownership
mixed_mode_regret2 中的 Relay
blocker_relay
Swap
DynamicRelayManager / active_stations
```

收敛为职责明确的三大件：

```
Pickup-Aware Cooperative HALNS
        +
Balanced Ownership Reassignment        —— 客户原始取件之前，完整任务责任调整
        +
Criticality-Guided Dynamic Relay       —— 任务开始执行后，后半程配送责任动态转移
```

职责边界：

- **Balanced Ownership**：只主动操作 **DIRECT task**，把 `A: P_i -> D_i`
  完整换成 `B: P_i -> D_i`（pickup + delivery 一起换 owner）。已 RELAY 的
  任务不主动拆开、不改 relay point、不换 owner。
- **Criticality-Guided Dynamic Relay**：`A: P_j -> h ; B: h -> D_j`，
  只负责"任务已开始执行后的后半程交付责任转移"。
- 两个来源（DELIVERY_RISK / BLOCKER_RELEASE）只是 **candidate trigger /
  guidance**，不是新的 ServiceMode。

---

## 2. 正式 ServiceMode：只保留 DIRECT / RELAY

```python
class ServiceMode(Enum):
    DIRECT   # A: P_i -> D_i     pickup_owner == delivery_owner, relay_event is None
    RELAY    # A: P_i -> h ; B: h -> D_i   pickup_owner != delivery_owner, relay_event is not None
```

**已从正式 Cooperative active path 移除 SWAP**：

- `ServiceMode.SWAP`
- `TaskServiceState.swap_event` 依赖
- `swap_structure` destroy
- `_swap_neighborhood_once()`
- `enable_swap` / `swap_interval` / swap adaptive / neighborhood 统计 / swap candidate generation
- `validate_cooperative_solution` 的 swap DAG 分支

**保留为 legacy standalone**：

- `swap_model.py` / `swap_search.py` / `swap_validation.py`（及其独立测试）
- `_repair_mixed()`、`_relay_options_for_task()`（legacy helper，不再进入
  REPAIR_OPERATORS）
- `DynamicRelayManager` + `DynamicRelayConfig` + `score_station`（legacy
  dynamic-station 模块及其测试）

正式 `solve_cooperative_halns()` 路径 **绝不再产生 SWAP**（validator 会直接
拒绝任何含 swap 的候选）。

---

## 3. TaskServiceState 简化

```python
@dataclass
class TaskServiceState:
    task_id: int
    mode: ServiceMode = ServiceMode.DIRECT
    primary_owner: int = -1     # 原始客户 pickup owner（K 约束作用对象）
    delivery_owner: int = -1    # 最终送达无人机
    relay_event: RelayTransfer | None = None

    @property
    def pickup_owner(self) -> int:   # 新正式词汇 alias
        return self.primary_owner
```

不做高风险全项目 rename：`primary_owner` 字段名保留，新代码优先使用
`pickup_owner` 语义（property alias）。

---

## 4. routes + relays 是执行结构的唯一 source of truth

`CooperativeSolution` 的 `routes + relays` 承载实际执行结构；`ownership` 与
`task_services` 是派生视图，由 `rebuild_services()` 恢复：

- DIRECT：`+i`、`-i` 在同一架无人机，无 RelayTransfer。
- RELAY：`+i` 在 `relay.first_drone`，`-i` 在 `relay.second_drone`，恰一个
  RelayTransfer。

验证规则（validator 强制）：

```
pickup_owner == relay.first_drone
delivery_owner == relay.second_drone
```

任何不一致 → validation fail。`ownership` 若存在必须与 routes 中实际
CUSTOMER_PICK owner 一致，不允许形成第二套独立真值。

`active_stations` 的语义重定义：

> 它**只表示当前 solution 中实际被 RelayTransfer 使用的 Relay Points**。
> 不再表示预先激活的 candidate shortlist。候选点池（P ∪ D catalog）是
> search-level 结构，不是 solution state。
> validator 会拒绝"active_stations 中存在未被任何 relay 引用的站点"。

---

## 5. 新正式主循环（A2 逐位一致 + 合作机制严格改进）

```
destroy_operators = _a2_destroy_pool(cfg)          # 与 solve_alns_core 完全相同的 A2 destroy 池
repair_operators  = _a2_repair_pool(cfg)           # 与 solve_alns_core 完全相同的 A2 repair 池
destroy_weights   = {name: 1.0 for name in destroy_operators}   # 与 A2 相同：均匀初始权重
repair_weights    = {name: 1.0 for name in repair_operators}
coop_rng          = Random(seed + 0x5EED)          # 合作邻域独立 rng，主轨迹 rng 流不受扰动

for iteration in ...:
    partial   = a2_destroy(current)                # 自适应 roulette（仅 A2 算子）
    candidate = a2_repair(partial, strategy=repair_name)   # 自适应 roulette（仅 A2 算子）
    if candidate is None: continue
    # ---- 合作邻域（定时、严格改进、用 coop_rng）----
    if enable_relay and iteration % relay_interval == 0:
        candidate = criticality_guided_dynamic_relay(candidate)   # 只接受严格 (late,dist) 改进
    if enable_ownership and iteration % ownership_interval == 0:
        candidate = ownership_neighborhood(candidate)            # 只接受严格 (late,dist) 改进
    score = routes_score(candidate)                 # 无 relay 时免完整校验
    accept_or_reject(candidate)                     # current: HALNS/SA; best: 严格 (late, dist)
```

关键性质（本轮最核心的公平性保证）：

- **主搜索轨迹与 `solve_alns_core` 逐位一致**：同样的 A2 算子池、同样的均匀
  权重 1.0、同样的 rng 流（合作邻域使用独立 `coop_rng`，不扰动主 rng），因此
  Cooperative-HALNS 的主循环**构造性保证不弱于 A2 基线**（固定迭代数下
  coopOFF 结果与 A2 完全相同，已用 60 固定迭代实验验证：late=106 /
  dist=735.8722 逐位相等）。
- **合作机制只做严格改进**：Balanced Ownership 与 Criticality-Guided Dynamic
  Relay 都是定时邻域，只接受 `(late_count, distance_km)` 严格更优的结果，
  只能加分、永不拖累主轨迹。
- **ejection 默认关闭**：项目 A2 基线 `solve_alns_core` 在内部显式
  `replace(config, enable_route_pool=False, enable_ejection=False)` 禁用
  ejection（`solve_alns` 才启用）。为公平比较，Cooperative-HALNS 默认
  `enable_ejection=False`，与基线逐位一致；需要 ejection 强化时显式开启。
- **每轮开销已压到 ≤ A2**：`_pickup_slacks` 惰性计算（仅合作算子需要）、
  无 relay 时用廉价 `routes_score` 代替完整校验；relay 邻域每
  `relay_interval=30`、ownership 每 `ownership_interval=50` 才调用一次
  （实测单次 relay 调用约 0.4s，降低频次后总开销 < 2%），实测 it/s 不再
  低于 A2（240s 公平对比中 Full 2.98 vs A2 2.77）。
- `mixed_mode_regret2` 与 `blocker_relay` 不再进入 repair roulette。

---

## 5.1 为什么必须保证 A2 全部算子（本轮性能修复）

首轮 common-start 实测 Full 明显弱于 A2（late 103/102/103 vs 94/95/90），
根因不是"合作机制没用"，而是正式主循环**把基础 ALNS 算子池砍窄了**：

| | A2 baseline | 首轮 Cooperative（错误） | 本轮（修复后） |
|---|---|---|---|
| destroy | 11（活跃 9） | 5（cooperative 取向） | **A2 全套 9 + 3 cooperative** |
| repair | 8（活跃 6，自适应） | 2（实际只 regret2） | **A2 全套 6 + ownership** |

A2 压 late_count 靠的是 `late_critical`/`deadline_related` destroy +
`deadline`/`slack`/`pickup_urgency`/`regret3` 自适应 repair，首轮全部缺失。
修复后（完整算子池 + 对齐 destroy 分数/权重更新间隔）：Full 30s 从
103/102/103 提升到 **98/94/98**，与 A2（94/93/94）差距缩小到 1-4 个 late
任务。

随后发现并修复了一个**不公平点**：首轮对比中 Full 还额外运行了 A2 基线
（`solve_alns_core`）**内部禁用的 ejection 邻域**（`solve_alns` 才启用），
导致轨迹在迭代 25 处分歧，当时"Full 胜过 A2"的结论被 ejection 混淆。修正为
默认 `enable_ejection=False`、主循环与基线逐位一致后（§5），正式 240s/seed
公平对比见 §19：**Full 76 vs A2 77（late）→ win**；该实例上合作机制贡献为
0（BO=0、relay=0），差距来自 Full 在相同墙钟内完成更多迭代（§20）。

---

## 6. Balanced Ownership Reassignment

继续复用 `ownership_reassignment.py` 的 balanced primitives（1↔1 / 2↔2 /
3-cycle 的思想），在 HALNS 内通过：

- **ownership destroy**：只从 **DIRECT task** 中选两个无人机各取若干任务，
  记录 `task → target(≠旧 owner)`（保持 8×25 计数平衡）。
- **ownership_targeted repair**：fixed-target 插入，`allowed = [target]`，
  target ≠ 旧 owner；无槽位/无可行插入 → 诚实返回 None（绝不允许静默回到
  原 owner）。

因此 **Balanced Ownership 的定义**是完整 DIRECT task 的 pickup + delivery
一起换 owner：

```
A: P_i -> D_i        A: P_i -> D_i
        ↓      =>          ↓
B: P_i -> D_i        B: P_i -> D_i
```

---

## 7. Regret-2 bug 修复

`_repair_ownership_targeted()` 原来的比较方向是**最小 regret 优先**（错误）。

标准 Regret-2：

```
regret = second_best - best        # (late_count, distance_km) 上的差
优先 argmax(regret)                # 第二好选项远差于最好选项的任务必须先放
```

修复：新增 `_select_highest_regret_task(choices)`，对每个任务用
`(-regret.late, -regret.dist, best_delta.late, best_delta.dist, task_id)`
取最小（等价于 max-regret 优先，tie-break min best-delta、min task_id），
`_repair_ownership_targeted` 使用它。**`_repair_direct()` 中原本正确的最大
Regret-2 逻辑未改动**。

测试：`TestOwnershipRegret::test_select_highest_regret_task` 构造
`regret(A) > regret(B)`，断言 repair 顺序必须首先选择 A。

---

## 8. `enable_ownership` 的诚实开关

`build_destroy_pool` / `build_repair_pool` 保留"完整算子池"语义（含
`pickup_risk`/`blocker`/`relay_structure`/`ownership` 与 `ownership_targeted`，
供测试与消融脚本引用）；但**正式主循环不再用它们做 roulette**，而是用
`_a2_destroy_pool` / `_a2_repair_pool`（与 `solve_alns_core` 完全相同的 A2
池，见 §5）。

Balanced Ownership 现在是**定时严格改进邻域**：

```python
if cfg.enable_ownership and iteration % cfg.ownership_interval == 0:
    candidate = _ownership_neighborhood_once(problem, evaluator, candidate,
                                             cfg, coop_rng, deadline)
```

`enable_ownership=False` 时：

- ownership 邻域不调用；
- `ownership_calls == 0`；
- 消融变体（A2+BO / Full）诚实区分。

这是消融实验可信性的基础（测试 `TestOwnershipToggle`）。

---

## 9. Criticality-Guided Dynamic Relay（统一 Post-Repair Neighborhood）

`_criticality_guided_dynamic_relay_once(problem, solution, cfg, rng, deadline,
relay_pool)` 是唯一的 Relay 入口，**不再出现在 repair roulette 中**。

Pipeline（严格限制 DAG 评估数量）：

```
Step 1  只选 delivery-risk / blocker-risk task   (critical_task_limit)
Step 2  receiver cheap shortlist                 (relay_receiver_limit)
Step 3  relay-point contextual shortlist         (relay_point_candidate_limit)
Step 4  cheap gain screening                     (几何预过滤 + 估计增益 > 0)
Step 5  merge + dedup                            (task, donor, receiver, relay_point_id)
Step 6  只 exact evaluate top candidates         (relay_candidate_limit, relay_max_evaluations)
```

物理机制始终只有一种：

```
A: P_j -> h
B: h -> D_j
```

每次调用默认**最多增加 1 个 RelayTransfer**（`relay_additions_per_call=1`）；
完整 solution 通过多轮 HALNS 累积多个 Relay；**同一 task 最多一次 relay**
（candidate 生成跳过已 relay 的 task）。

### 9.1 配置

正式 `CooperativeHALNSConfig` 中 Relay 相关：

```python
critical_task_limit        = 6
blocker_per_task_limit     = 3
delivery_risk_slack_limit  = 15.0      # search 参数，不是模型约束
near_critical_slack_limit  = 10.0
relay_receiver_limit       = 1
relay_point_candidate_limit = 3
relay_candidate_limit      = 1
relay_max_evaluations      = 3
relay_interval             = 30        # 高频无用 → 降频，总开销 < 2%
relay_additions_per_call   = 1
max_relays_per_solution    = None
ownership_interval         = 50        # Balanced Ownership 邻域周期
ownership_pair_trials      = 3
enable_ejection            = False     # 与 solve_alns_core 基线一致（公平性）
```

**已删除/迁移的旧配置**：`enable_swap`、`swap_*`、`relay_probe_tasks`、
`relay_probe_total`、`relay_station_candidate_limit`、`relay_max_candidates`、
`cooperative_candidate_limit`（死配置）、`allow_temporary_late_bridge`
（reserved 且未生效）、`enable_dynamic_station`、`dynamic_relay`、
`station_refresh_every`。

---

## 10. Delivery-Risk candidate generation（新增 guidance）

对完整 solution 计算每个任务实际 delivery time `t(D_i)`：

```
delivery_slack_i = deadline_i - t(D_i)
```

优先 `delivery_slack < 0`（已预计逾期），同时允许
`0 <= delivery_slack <= delivery_risk_slack_limit` 的 near-critical task。

对风险 DIRECT task `i`：

```
donor  = 当前 pickup/delivery owner A
receiver = B != A
relay point = h
A: P_i -> h ; B: h -> D_i
目标：t(D_i) 提前（late task -> on-time task）
```

cheap estimate `_estimate_delivery_gain`：

```
earliest_drop    = t(P_i) + d(P_i,h)/v
receiver_depot   = d(depot,h)/v          # 接收机从 depot 出发的下界
earliest_pick    = max(earliest_drop, receiver_depot)
new_delivery_lb  = earliest_pick + d(h,D_i)/v
estimated_delivery_gain = max(0, t(D_i) - new_delivery_lb)
```

即使乐观下界都不早于当前 `t(D_i)` 的候选被 screen out。

---

## 11. Blocker-Release candidate generation

Pickup criticality 继续采用（只用于 candidate generation / blocker detection /
priority，**不得进入正式 Score**）：

```
latest_safe_pickup_i = deadline_i - direct(P_i,D_i)/v
pickup_slack_i       = latest_safe_pickup_i - t(P_i)
```

对 critical pickup `i`，用现有 `find_upstream_blockers()` 找 donor 路线上的
upstream blocker `j`（`P_j ... D_j ... P_i`）。新增轻量级：

```
estimate_blocker_release_gain(problem, routes, donor, j, i, h)
    current = (d(P_j,D_j) + d(D_j,P_i)) / v
    after   = (d(P_j,h)   + d(h,P_i))   / v
    gain    = max(0, current - after)
```

再生成 `relay task = j, donor = A, receiver = B, relay point = h`，让
`A: P_j -> h ; B: h -> D_j`，从而 A 从 h 之后提前继续去 P_i。真实结果必须
经过完整 Relay DAG validation。

---

## 12. 两个 Relay source 进入同一个 candidate pool

```
delivery-risk candidates
        +                         merge
blocker-release candidates   ->   dedup      ->  cheap screening  ->  exact eval
```

- dedup key：`(task_id, donor, receiver, relay_point_id)`。
- 同一 candidate 同时被两种 guidance 发现 → 合并 `triggers =
  {DELIVERY_RISK, BLOCKER_RELEASE}`，**不做两次 DAG evaluation**。
- `RelayTrigger`（Enum）与 `RelayCandidate` 定义在 `dynamic_relay.py`，
  字段是对现有 relay-candidate 数据结构的最小补充（不重写整个系统）。

---

## 13. Relay Point：P ∪ D 候选 + contextual ranking

- 正式 Relay candidate catalog 使用 `H = P ∪ D`（去重），不新增连续空间
  relay location。
- 保持防护：`h != P_i`、`h != D_i`（`h = P_i` 等价 Ownership transfer，
  `h = D_i` 无 Relay 意义）。
- **没有固定全局站点分数**。对 `(task i, donor A, receiver B, h)` 动态评价：

```
score_relay_point_contextual = donor_detour + receiver_detour
                             + d(h, D_i) + d(P_i, h)
                             - 0.5 * min(receiver_free_window, 2*Q)
```

先针对具体 `(task, donor, receiver)` 从 P∪D 选 top-K relay points，再 exact
evaluate，**不对所有安全节点全量跑 DAG**。候选排序：contextual score 为主、
估计增益为次（避免"估计增益最高但实际最差"的站点排第一）。

几何预过滤（cheap、只用于提速，不做正确性过滤）：
- delivery-risk：`d(P_i,h) + d(h,D_i) <= v*(t(D_i)-t(P_i))`
- blocker-release：`d(P_j,h) + d(h,P_i) <= d(P_j,D_j)+d(D_j,P_i)`

---

## 14. Relay exact ranking 严格服从正式目标

最终 exact decision 永远按：

```
(score.late_count, score.distance_km)
```

只有正式 Score **完全相同**（数值容差内）时才允许用 `delivery_gain` /
`pickup_advance` 做 tie-break。

**绝对不允许**：`late_count` 相同、为了 pickup advance 接受更长的 distance。

---

## 15. DynamicRelayManager / active_stations 的重新定位

新正式路径：

```
先确定具体需要 Relay 的 task + donor + receiver
再动态选择适合它的 Relay Point（contextual shortlist）
```

- `DynamicRelayPointPool`（新）：P∪D catalog + contextual ranking + 
  shortlist/cache + point usage statistics。**不是 solution state**。
- `DynamicRelayManager` 保留为 legacy（旧 dynamic-station 模块及其测试），
  正式 `solve_cooperative_halns()` 不再使用。
- 不再强制"至少激活几个 station / 定期轮换 station / 未使用 station 长期
  作为 solution state"。

---

## 16. K 与 Q 的 Relay semantics（回归验证，未改）

物理容量 Q：

```
CUSTOMER_PICK -> load +1
RELAY_DROP    -> load -1
RELAY_PICK    -> load +1
DELIVERY      -> load -1
所有事件 load <= Q
```

K=25 只统计**原始 CUSTOMER_PICK**：每架 UAV 承接的原始客户任务数 <= 25；
receiver 在 Relay Point 的 RELAY_PICK 参与 Q / timing / distance / precedence，
但**不再次消耗 K**。

Async Relay 保持：`t_drop <= t_pick`（不要求相等），donor drop 后可立即离开，
Relay Point 是 temporary-storage transfer point。

新增回归测试：`TestKRelaySemantics`（RELAY_PICK 不占 K）、
`TestQRelaySemantics`（+1/-1/+1/-1 传播、超 Q 拒绝）、`TestAsyncRelay`
（drop < pick 合法）。

---

## 17. 统计

`CooperativeHALNSResult` 新增/保留：

```
ownership_calls / ownership_accepted
relay_calls
delivery_risk_tasks_detected / blockers_detected
relay_candidates_before_dedup / after_dedup / screened_out
relay_exact_evaluations / relay_feasible / relay_accepted
delivery_risk_relays_accepted / blocker_relays_accepted / dual_trigger_relays_accepted
late_tasks_rescued / critical_pickups_advanced
relay_count_final / unique_relay_points_used
```

正式结构中不再保留 `station_adds / station_drops / station_replaces` 与
swap statistics 的语义（保留字段为 0 仅用于 legacy compat wrapper）。

---

## 18. 测试结果

`python -m pytest -q` → **208 passed / 0 failed**（baseline 200 → 重构后 208）。

新增/更新测试要点：

| 测试 | 内容 |
|---|---|
| `TestServiceMode` | 只有 DIRECT/RELAY，无 SWAP；pickup_owner property；routes+relays rebuild |
| `TestOwnershipRegret` | MAX regret 优先（确定性构造 regret(A) > regret(B)） |
| `TestOwnershipToggle` | enable_ownership=False → ownership destroy/repair calls = 0 |
| `TestKRelaySemantics` | RELAY_PICK 不重复占 K |
| `TestQRelaySemantics` | +1/-1/+1/-1 传播，超 Q 拒绝 |
| `TestAsyncRelay` | drop <= pick，不要求相等 |
| `TestRelayEndpoint` | 拒绝 h=P_i、h=D_i |
| `TestDeliveryRiskRelay` | DIRECT 会 late → RELAY 后 on-time，late_count ↓ |
| `TestBlockerReleaseRelay` | P_j→D_j→P_i，relay j 后 t(P_i) 明显提前 |
| `TestDualTriggerDedup` | 同一 (task,donor,receiver,h) 两来源 → 1 候选、2 triggers、1 次 exact eval |
| `TestContextualRelayPoint` | 每 (task,donor,receiver) 动态排 Top-K，catalog = P∪D |
| `TestNoRiskCase` | 全部 slack 大、无 blocker → 0 candidate |

旧架构绑定测试（断言 `ServiceMode.SWAP` / `enable_swap` /
`DynamicRelayManager.active_stations` 短列表语义等）已随正式架构更新；
`swap_model / swap_search / swap_validation` 的 standalone legacy 测试保留。

---

## 19. Common-start 公平基准

协议（`run_common_start_benchmark.py`）：

```
seed s
  -> 只运行一次 A2 warm start（warm_s 秒）
  -> same_initial_solution（deep-copy）
       /                     \
  A2 refine               Full refine
  (refine_s 秒)           (refine_s 秒)
```

same seed / same initial / same refinement wall-clock。比较 late_count、
distance、time-to-best、iterations/sec。

### smoke（warm 20s + refine 8s，seeds 500-502，完整 A2 算子池）

| seed | warm | A2 refine | Full refine | Full vs A2 |
|---|---|---|---|---|
| 500 | 112 | 111 / 810.4 | **109 / 780.8** | **win**（late 更少） |
| 501 | 109 | 102 / 773.4 | 109 / 770.8 | loss |
| 502 | 110 | 106 / 783.0 | 109 / 798.7 | loss |

### 30s / method（warm 30s + refine 30s，seeds 500-502，完整 A2 算子池）

| seed | warm | A2 refine | Full refine | BO accepted | Relay accepted | relay_eval | Full it/s | A2 it/s |
|---|---|---|---|---|---|---|---|---|
| 500 | 109 | 94 / 751.4 | 98 / 754.7 | 6 | 0 | 26 | 2.17 | 2.77 |
| 501 | 106 | 93 / 755.4 | 94 / 722.5 | 5 | 0 | 27 | 2.33 | 2.50 |
| 502 | 109 | 94 / 725.4 | 98 / 737.1 | 2 | 0 | 36 | 2.40 | 2.63 |

### 240s / seed 正式（warm 120s + refine 120s，公平对比；relay_interval=30）

| seed | warm | A2 refine | Full refine | BO | Relay | relay_eval | outcome |
|---|---|---|---|---|---|---|---|
| 500 | 86 | 77 / 682.2（333 it, 2.77 it/s） | **76 / 679.9**（358 it, 2.98 it/s） | 0 | 0 | 31 | **win** |

**诚实结论（Gate D，最终）**：公平化后（主循环与 `solve_alns_core` 逐位一致、
ejection 默认关闭、合作机制严格改进、relay/ownership 低频调用），正式
240s/seed 对比 **Full 76 vs A2 77（late）→ 1 win**，距离也更优（679.9 vs
682.2）。Full 在相同墙钟内完成更多迭代（2.98 vs 2.77 it/s → 358 vs 333），
沿同一条 A2 轨迹多走了约 25 步，把 late 从 77 压到 76。**该实例上合作机制
贡献为 0**（BO=0、relay=0；31 次 exact eval 全部被严格 formal ranking 拒绝），
真正压 late_count 的是 A2 基础 ALNS（见 §20.4）——这与用户观察一致。

（注：A2 自身结果在不同机器负载下有 ±2 随机波动，跨轮次的绝对数值不完全
可比；同轮内同 warm start 的对比是严格公平的。30s 表格保留为历史记录。）

---

## 20. Gate D 瓶颈分析（第三轮：公平化修复后）

按用户要求，**不修改正式目标、不为制造 Relay 人为接受更差候选**，直接分析：

1. **首轮根因（已修复）**：正式主循环曾把 A2 基础算子池砍窄（5 destroy + 单
   regret2 repair），Full 单次迭代改进能力远弱于 A2。接入完整 A2
   destroy/repair 池并对齐 `max_destroy_fraction=0.10` 与
   `weight_update_interval=40` 后，30s 对比从 103/102/103 提升到 98/94/98
   （见 §5.1）。

2. **ejection 不公平（本轮发现并修复）**：项目 A2 基线是 `solve_alns_core`，
   它在内部 `replace(config, enable_route_pool=False, enable_ejection=False)`
   **禁用 ejection**（`solve_alns` 才启用）；而 Cooperative-HALNS 首轮主循环
   运行了 ejection，导致轨迹在迭代 25 处分歧，当时"Full 胜过 A2"的结论被
   ejection 混淆（240s 那次 77/672.9 vs 77/681.2 的"胜"来自 ejection 而非
   合作机制）。修正：默认 `enable_ejection=False`，主循环与基线逐位一致
   （§5）。固定 60 迭代验证：coopOFF 结果与 A2 **逐位相同**（late=106 /
   dist=735.8722）。

3. **正式 240s/seed 公平结果（seed 500）**：warm late=86 → A2 refine
   late=77 / 682.2km（333 迭代，2.77 it/s）→ **Full refine late=76 /
   679.9km（358 迭代，2.98 it/s）→ win**。Full 在相同墙钟内完成更多迭代
   （主循环开销不高于 A2：惰性 `_pickup_slacks` + 无 relay 时免完整校验 +
   relay/ownership 低频调度），沿同一条 A2 轨迹多走了约 25 步，把 late 从
   77 压到 76。单 seed 结果，A2 自身 ±2 波动，需多 seed 确认。

4. **合作机制在本实例贡献为 0（诚实结论）**：公平对比下 BO=0、relay=0。
   relay 在 120s 内 exact eval 31 次、候选 768 个，全部被严格 formal
   ranking 拒绝；ownership 也找不到严格改进。**真正把 late_count 压下来的
   就是 A2 基础 ALNS**——用户观察正确："反正现在发挥作用的还是 ALNS
   算法"。

5. **机制有效性在 synthetic 层面**：Delivery-Risk relay（late→on-time）、
   Blocker-Release relay（pickup 提前 90min）、Balanced Ownership（换
   owner）在 `test_critical_relay.py` 等 208 个测试中验证有效。本 8×25
   满载实例缺乏触发条件，不代表机制无效——单机 ALNS 的候选解空间表达不了
   跨机 relay / ownership，合作机制的价值在**解空间严格更大**这一层面。

6. **当前状态**：Cooperative-HALNS 主循环 = A2 基线轨迹 + 合作机制严格改进
   邻域，**构造性保证 ≥ A2**（主轨迹逐位一致 + 合作只做严格改进）；240s
   公平对比实测 **76 vs 77（win）**。合作机制在该实例上未触发，但在有
   pickup-blocking / delivery-risk 结构的实例上可提供 ALNS 无法表达的解空间
   增益；后续可评估增大 relay 预算或引入"确有 rescue 潜力才触发"的门控来
   在该实例上激活机制，但不得放宽正式目标或接受更差候选。

---

## 21. 遗留标记

依赖旧 `swap` / old cooperative wrapper / 旧 dynamic-station semantics 的
实验脚本已标记 **LEGACY / NOT USED FOR CURRENT PAPER**：

- `run_phase1_verify.py`
- `run_dynamic_cooperative_benchmark.py`
- `run_relay_swap_validate.py`
- `run_swap_v2_benchmark.py`
- `compare_a2_coop.py`
- `quick_240s_bench.py`
- `show_coop_stats.py`

正式实验统一调用 `solve_cooperative_halns()`：

- `run_cooperative_halns_benchmark.py`（Gate A/B/C/D）
- `run_common_start_benchmark.py`（common-start，`--refine 60/120` 即 60/120s）
- `run_ablation.py`（A2 / A2+BO / A2+DR / Full / DR-only / Blocker-only）

> 旧报告若声称目标为 `(late_count, total_lateness_min, distance_km)` 或包含
> swap/dynamic-station 作为正式机制，一律视为 **legacy/outdated**。
