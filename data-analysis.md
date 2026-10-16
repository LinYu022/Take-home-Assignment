# LeRobot 数据分析报告

**证据口径：** 以改后接入工具层的独立 Agent 实验为主，以确定性 HTTP 客户端和离线脚本对关键数字、范围、URL 和 PNG 做复核。  

## 0. 结论摘要和范围

本报告覆盖当前快照的唯一 episode，所有“全量”均指这一 episode，不代表数据集总体或其他采集批次。数据层、视频层和 Agent 工具层分别核对：数据表共 3029 行、16 列；包含 10 个向量字段和 6 个标量/索引字段；视频为 640×480 H.264/YUV420P，共解码 3029 帧；任务为 `Sew fabric pieces`。对象总大小为 38,899,541 B（约 37.10 MiB）。

| 主题 | 已确认结果 | 性质与来源 |
| --- | --- | --- |
| 数据规模 | 1 episode、3029 帧、1 task、1 视频、16 列、1 个 Parquet row group | 事实；`describe_dataset` 与确定性全量扫描 |
| 时间轴 | `timestamp_ns` 严格递增；相邻间隔只有 33,366,666/33,366,667 ns；数据最后一帧为 101.034266666 s | 事实；`audit_timing` 与 `metrics.json` |
| 视频覆盖 | 3029 帧全部可解码，PTS 严格递增，time base 为 `1/979001`，关键帧间隔均为 4 帧 | 事实；视频全量解码与 PTS 审计 |
| 跨模态关系 | `action.left_hand_tracks[0][t] = left_hand/tracks[1][t+1]` 在 `[0,3028)` 的 3028 个配对全部精确匹配 | 事实；`compare_series`；仅限一个字段、一个分量和本 episode |
| 完整性 | 审计字段无 null/非有限值，向量长度稳定；左 action 只有末行是精确全零向量 | 事实；`audit_integrity` |
| 投影质量 | 全部 42 个点均在相机前方；3009 帧为 42/42 个点在标定图像内，20 帧为 41/42 | 事实；确定性投影扫描；不等于可见性或像素标定精度 |
| 能力边界 | 当前不能回答总体成功率、泛化、子任务边界、力/接触/深度、点名称、可见性或 MANO betas 成因 | 事实；字段目录和缺失证据清单 |

报告中的数字结论均带有覆盖范围；“事实/合理推断/待验证假设”和“Agent 得出/人工核对/确定性代码复核”是两套独立标签。原始 `view_url` 保存在 Agent 调用记录和本报告附录中，需要在同一数据快照服务上打开；更换端口时使用服务端 `/api/view/normalize` 映射，不手工改写 token。

| 作业要求 | 对应章节 | 当前覆盖 |
| --- | --- | --- |
| 数据结构、规模、字段语义 | C1、C2、摘要表 | episode/task/视频规模、16 列、声明与实际存储类型、坐标系、单位来源、向量布局 |
| 统计分布 | C3、C6、统计分布章节 | 有界字段统计，以及时间、运动、视频和投影的全量确定性分布 |
| 不同模态关系 | C4、C5、C7 | action/observation 下一帧配对、数据 timestamp/视频 PTS、投影与 PNG manifest |
| 完整性、一致性、异常和质量 | C5、C6、统计分布章节 | 时间单调性、PTS 覆盖、长度/null/非有限/全零、投影画外候选和运动候选 |
| 对训练、评估、采集和治理的价值 | 建议章节 | 下一帧监督防泄漏、评估边界、契约字段、标注和采集补充项 |
| 无法回答的问题 | 能力边界章节 | 成功率、泛化、阶段、接触/力/深度、可见性和 MANO 成因均明确列为缺失证据 |

### 证据定位

C1–C7 的 URL 用于复核相应的字段、范围或画面；C8 的四元数数值属于历史确定性补充结果，其 overview URL 只能确认快照范围，不能单独证明历史数值。附录逐字保存可用 URL，便于评审者复制到同一快照服务中复核；图像结论另附 PNG 字节数和 SHA-256。`metrics.json` 只承载确定性扫描结果，不被当作 Agent 的工具响应。

## 1. 方法和证据层级

本轮 Agent 使用改后工具目录和 `AGENTS.md`，没有读取仓库源码或原始 Parquet/MP4。

主 Agent 账本实际记录 **21 次业务调用、48,389 个 UTF-8 `content.text` 字节**，上限为 40 次和 204,800 字节。工具发现、协议、完整 MCP 对象、wire 和 PNG 单独计量。

确定性复核使用独立客户端实际调用 16 次、62,485 个 UTF-8 字节。它没有被计入 Agent 主预算，也不冒充 Agent 的回答。复核代码重新检查了数据目录、分页、完整配对、时间、完整性、证据范围和两张图片。

每条结论下列明性质、来源、覆盖范围和限制。原始请求、响应、`view_url`、重放参数和客户端收到的内容以 JSONL 形式保存；最终 URL 需在同一数据快照服务上打开，换端口时应通过 `/api/view/normalize` 映射，不能手工改 token。

## 2. C1：数据范围、任务和来源

**性质：事实。**  
**主来源：** Agent 第 2 次业务调用 `describe_dataset(field_limit=32)`。  
**确定性复核：** HTTP 账本调用 1 的 `structuredContent` 和快照摘要。

当前快照报告：1 个 episode、3029 帧、1 个 task、1 个视频，声明 fps 为 29.97，`robot_type=human_egocentric`。任务原文为 **Sew fabric pieces**，任务描述涉及引导、对齐并缝合蓝色布料、绕梭芯和剪线。

episode 0 的成功值为 `true`，来源为 `meta/custom_metadata.csv:episode_index=0:success`，状态为 `metadata_declaration`。这证明存在一条元数据声明，不证明物理任务成功，也不能由单个 episode 推出成功率或泛化能力。

数据采样域和视频域均记录为半开区间 `[0,101067633333)` ns。端点相同只表示 manifest 中的时间范围相同，不证明视频内容和数据行已经完成像素级对齐。

## 3. C2：字段声明、存储和语义

**性质：字段语义是 metadata/工具声明；观测长度和异常数量是确定性审计事实。**  
**来源：** Agent 的 `describe_dataset`、`AGENTS.md` 语义规则，以及字段目录确定性复核。

| 字段类别 | 声明和实际存储 | 可用解释 |
| --- | --- | --- |
| `timestamp`、`timestamp_ns`、`frame_index`、`episode_index`、`index`、`task_index` | 标量；声明为 float32 或 int64，Arrow 存储分别为 `float` 或 `int64` | `timestamp_ns` 是精确同步键；float32 `timestamp` 只适合显示 |
| `base_0_camera/position` | float32 `[4]`，Arrow `list<element: double>` | raw `[ts_ns,x,y,z]`；world 坐标，XYZ 单位为 m，单位来自 description |
| `base_0_camera/quaternion_xyzw` | float32 `[5]`，同样为 double list | raw 0 是时间戳前缀；payload 单位未声明 |
| `left_hand/tracks`、`right_hand/tracks` | float32 `[64]` | raw `[ts_ns,21×(x,y,z)]`，raw 0 为时间戳前缀，world 坐标，payload 单位未声明 |
| `observation.*_keypoints_quaternion` | float32 `[84]` | 21 个四元数；无额外单位推断 |
| `action.*_hand_tracks` | float32 `[63]` | 21×3 的无时间前缀 action 目标；raw component 0 是第一个空间分量 |
| `observation.left_hand_mano`、`observation.right_hand_mano` | float32 `[61]`，无时间戳前缀，camera 坐标 | raw `[0,3)` global orientation、`[3,48)` hand pose、`[48,58)` betas、`[58,61)` translation；混合向量没有统一单位声明 |

`observed_length` 只表示 episode 0 物理 row 0 的抽样长度，不是全表结构审计；全表长度分布由 `audit_integrity` 给出。字段目录保留了原始 `description`、`frame`、声明 dtype/shape、Arrow `storage_dtype`、timestamp 前缀、component layout 和单位来源。未知单位、点名称、骨架拓扑和可见性保持未知。

## 4. C3：分页查询和有限统计

**性质：事实。**  
**主来源：** Agent 第 3–6 次业务调用。  
**确定性复核：** HTTP 账本保存同一查询的页范围、evidence 和完整返回对象。

Agent 选取 `action.left_hand_tracks` 的 raw component 0，原始帧范围 `[1513,1518)`、`limit=2`，只改变 cursor 续查：

| 页 | 返回范围 | 值 |
| --- | --- | --- |
| 1 | `[1513,1515)` | −0.06408820047336763，−0.0644577667100996 |
| 2 | `[1515,1517)` | −0.06486052679346138，−0.06500560519813839 |
| 3 | `[1517,1518)` | −0.06462483191193186 |

三页合并后为 5 个值；同范围 `aggregate_series` 返回 `exact=true`、`input_count=5`，统计为：

| count | min | max | mean | p50 | p95 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 5 | −0.06500560519813839 | −0.06408820047336763 | −0.06460738621739978 | −0.06462483191193186 | −0.06416211372071402 |

百分位数是在完整输入范围上计算的，没有把各页的局部分位数再平均。该统计只针对一个字段、一个 component 和一个 episode，不代表整只手或整段所有分量。

## 5. C4：action 与 observation 的下一帧关系

**性质：事实。**  
**主来源：** Agent 第 7 次 `compare_series`。  
**确定性复核：** HTTP 客户端重新执行同一有界比较，并另做三个窗口的逐值比较。

比较定义为：左侧 `action.left_hand_tracks` raw 0 的 `A[t]`，右侧 `left_hand/tracks` raw 1 的 `O[t+1]`，`right_frame_offset=+1`，左侧范围 `[0,3028)`。结果为：

| expected | found | finite | matched | mismatch | missing | duplicate | nonfinite |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 3028 | 3028 | 3028 | 3028 | 0 | 0 | 0 | 0 |

MAE、RMSE 和最大绝对误差均为 0；比较使用有限 binary64 数值精确相等，`+0` 与 `-0` 相等。确定性复核与 Agent 结果一致。独立客户端还检查了 `[0,3)`、`[1513,1516)`、`[3025,3028)` 三个窗口，共 9 个帧对、567 个空间标量对，全部相等。

这只证明一个 action 字段的一个 raw component 和一个 episode 范围。它不证明其余 62 个 component、右手、其他字段、其他 episode 或动作生成机制。

训练解释必须避免未来信息：`O[t+1]` 可以作为对齐后的目标核验，但不能作为预测 `A[t]` 时刻的输入。输入应限制为允许的观测 `≤t`；没有下一帧目标的末行应从目标损失和目标统计中单独处理，同时保留末行观测。

## 6. C5：时间同步、PTS 和帧覆盖

**性质：事实；解释边界为编码时间关系。**  
**主来源：** Agent 第 8 次 `audit_timing`。  
**确定性复核：** HTTP 账本完整扫描 `[0,3029)`。

- timestamp 检查 3029 行，无无效 timestamp、重复间隔或倒退间隔。
- 3028 个相邻间隔为 33,366,666–33,366,667 ns，声明的 `frame_period_ns=33,366,667` 是中位帧周期，不代表每个间隔恒定。
- PTS 检查 3029 帧，`missing=0`，状态 `complete`；PTS 重复和倒退均为 0。
- `time_base=1/979001`；换算版本为 `rational_ns_v1`，使用精确有理数和最近整数 ties-to-even。
- `timestamp_ns - pts_ns` 的 count=3029，min=−1 ns，max=103 ns，mean=51.21558270056124 ns。

这支持采样时间与视频 PTS 的数值编码关系。它不证明采集时钟精度、场景内容同步、接触时刻、漂移原因或图像像素对齐。

## 7. C6：完整性、全零向量和 MANO

**性质：完整性统计为事实；物理含义是不可由数据单独确定的推断。**  
**主来源：** Agent 第 9 次 `audit_integrity`。  
**确定性复核：** 对指定字段完整覆盖 `[0,3029)`，并用末帧 query 单独核对。

| 字段 | 长度分布 | null / 非有限 | 精确全零向量 | 均值为零但非全零 |
| --- | --- | ---: | ---: | ---: |
| `action.left_hand_tracks` | 63：3029 行 | 0 / 0 | 1 | 0 |
| `observation.left_hand_mano` | 61：3029 行 | 0 / 0 | 0 | 0 |
| `observation.right_hand_mano` | 61：3029 行 | 0 / 0 | 0 | 0 |

`query_series` 对 frame 3028 的 action 行确认 63 个 raw component 全为 0；指定范围中此前非末行 raw component 0 的最大值为 −0.027468378466734722。结合全段只有一个全零 action 向量，可以把该全零向量的位置确定为末行，但“sentinel”是数据/指南语义，不能进一步解释为静止、终止、奖励或任务成功。

MANO 向量的长度和有限性通过了指定字段审计，但没有提供 betas 为零的生成原因。历史 `metrics.json` 另报告过两侧 MANO `betas[48:58]` 全零；该结果属于历史确定性产物，不是本轮 Agent 新审计，也不能据此解释模型或采集流程的成因。

## 8. C7：渲染、投影和视觉观察

**性质：manifest 数字为确定性事实；图像内容为 Agent 观察，不是人工核对。**  
**主来源：** Agent 第 18–21 次 `render_view`、`decode_view` 和最终 URL 复核。  
**确定性复核：** 两张 frame 3025 PNG 的实际字节、SHA-256、manifest 和 evidence 对照。

单变量视图使用 frame 3025 的 query evidence `[3025,3026)`，series 为 `action.left_hand_tracks`，左右 observed hand overlay，尺寸 1024×768；两个最终 canonical state 只改变 `display.info_bar`。

manifest 报告：

- `range_source=query_page`，`range_start_frame=3025`，`range_end_frame=3026`，没有扩大范围；
- overlay 源字段为同帧 observed `left_hand/tracks`、`right_hand/tracks`，不是所选 action；
- `total/projected=42`、`drawn=42`；`nonfinite=0`、`behind_camera=0`、`outside_image=0`；
- calibration image 和 decoded video 均为 640×480，`video_rect=[16,16,605,454]`，scale 约 `[0.9453125,0.9458333]`，translation `[16,16]`，crop 为 null；
- 曲线单点均值约 0.1933447484，单位为 `Unknown unit`。

`outside_canvas=0` 的含义是非有限、相机后方和超出标定图像中心三类均为 0，不是整张 PNG 边界的判断。`drawn=42` 不证明点可见、无遮挡、属于有效场景 ROI 或已经完成像素级标定。

Agent 描述实际看到了缝纫机、蓝色布料、双手和彩色点；信息栏关闭后暗带仍存在，部分绿色点位于暗带内。该描述是 Agent 的图像观察，不能写成人工视觉核对，也不能据暗带推断黑边、无效区域、丢帧或内容错位。

两张图的实际哈希：

- 信息栏开启：`5aba32f4e3c9d1d4a2d5f39f315af43fc68916308ff1e3721423431775bc0ea4`，441,102 字节；
- 信息栏关闭：`87befb44a4adfdd351414227c13c963a87b92454b250605da4636b2d47ea91ee`，438,571 字节。

## 9. C8：历史四元数补充结果和能力边界

作为补充材料，历史确定性 `metrics.json` 报告过相机、左手和右手四元数范数最大绝对误差分别为：

- camera：`1.1102230246251565e-16`；
- left：`3.3306690738754696e-16`；
- right：`2.220446049250313e-16`。

这些数字是历史脚本结果，未在本轮 Agent 会话中重新计算；它们只能说明历史产物中的范数检查结果，不证明当前所有字段或未来数据集都满足同样性质。

## 10. 统计分布和质量信号

下面的统计来自历史确定性全量扫描 ，覆盖 episode 0 的全部 3029 行或全部 3029 个视频帧；它们没有被本轮 Agent 重新计算，因此来源标为“确定性代码复核”，不能写成 Agent 直接观察。

### 时间和数值表示

| 指标 | 结果 | 解释边界 |
| --- | --- | --- |
| `timestamp_ns` 相邻差 | 33,366,666 ns：1010 次；33,366,667 ns：2018 次 | 采样间隔高度稳定；`frame_period_ns` 是中位数，不代表每个间隔相同 |
| 名义/整数时间推导 FPS | 29.97 / 29.970029970227728 | 只描述存储时间轴，不是采集时钟精度证明 |
| float32 `timestamp` 相对 ns 的最大误差 | 3.812996098417898×10⁻⁶ s | 展示字段存在微秒级量化差异；精确同步仍用 int64 `timestamp_ns` |
| 向量内嵌 timestamp 的最大误差 | 0 ns | 仅适用于带前缀的四个向量；不说明未带前缀字段 |
| 纳秒转 float32 的最大误差 | 4096 ns | 不应把纳秒时间戳先转 float32 再做同步键 |

### 相机和手部运动范围

| 对象 | 全量范围/分布 | 质量信号 |
| --- | --- | --- |
| 相机平移 XYZ（m） | min `[-0.0581984,-0.0747555,-0.0000128]`；max `[0.1173241,0.0191053,0.1263248]` | 范围是观测到的 world 坐标，不是安全工作空间或物理边界 |
| 相机相邻步长（m） | p50 `0.0006122`，p95 `0.0038582`，p99 `0.0050380`，max `0.0057942` | 可作为后续异常阈值讨论的基线；当前没有标注阈值，不能把最大步长定为错误 |
| 左手世界坐标（m） | min `[-0.2442303,-0.0991310,0.2146540]`；max `[0.1073419,0.2270265,0.6129398]` | 仅为观测范围，不是人体可达范围证明 |
| 右手世界坐标（m） | min `[-0.0476086,-0.0163761,0.2218653]`；max `[0.2969174,0.2812814,0.5812211]` | 同上 |
| 每帧平均关节步长（m） | 左手 p50 `0.0026181`、p95 `0.0181514`、max `0.0321097`；右手 p50 `0.0018773`、p95 `0.0146795`、max `0.0305379` | 描述运动分布；不等于速度，速度还需明确时间间隔 |
| 单个关节最大步长 | 左 `0.0489059`（转场 129→130，点 16）；右 `0.0678617`（转场 774→775，点 12） | 候选质量检查点；没有真值或阈值时只能标为待复核候选 |

历史扫描没有发现任一手部关节相邻跳变超过 0.1 m，也没有连续重复轨迹。这个结果支持“没有被当前阈值筛出的明显大跳变”，不能证明没有遮挡、错标或小幅系统偏差。

### 视频和投影分布

视频容器时长为 101.067633 s，最后一帧 PTS 约为 101.034267 s；容器尾部与最后采样行之间的差异符合半开时间域处理。PTS 与 `timestamp_ns` 的最大差为 103 ns，而与 float32 `timestamp` 的最大差约为 3.893×10⁻⁶ s。视频帧数与数据行数一致，不能进一步推出画面内容和标注在语义上逐像素同步。

投影扫描得到 3009 帧的 42/42 个点在 640×480 标定图像中心范围内，20 帧为 41/42；所有点均在相机前方。20 帧集中在 2103–2107 和 2707–2721。这里的“画外”是投影中心超出标定图像范围，不能解释为 PNG 黑边、遮挡、不可见或内容错误。应把这些帧列为后续人工或 2D 标注复核候选，而不是自动删除。

## 11. 对训练、评估、采集和治理的建议

以下是基于上述事实的工程建议，属于建议或待验证假设，不是数据本身新增的事实。

### 训练

- 使用 `timestamp_ns` 作为同步键，并保留原始向量 dtype/分量索引；不要把包含纳秒前缀的向量整体取均值，也不要把 MANO 混合向量统一标成一个空间单位。
- 对 action 监督使用明确的下一帧配对：输入只能使用时刻 `t` 及以前允许的观测，`A[t]` 的目标可以是 `O[t+1]`；最后一行没有下一帧目标，应从目标损失和目标统计中 mask，同时保留最后一行观测。
- 训练前执行长度、null/非有限、时间单调性和全零向量审计。全零 action 末行应作为数据契约处理，不能直接当作静止、终止或成功标签。

### 评估

- 当前只有一个 episode，不能进行可靠的跨 episode 泛化、成功率或训练/评估切分结论。至少需要更多独立 episode 或外部评估集。
- 报告 action/observation 对齐时必须同时写明字段、raw component、帧偏移、覆盖范围和有限配对数；一个分量的全量匹配不能推广到 63 个分量或另一只手。
- 把 PTS 与 `timestamp_ns` 的差异作为编码一致性指标，不能把它当作场景接触时刻、视觉内容偏移或捕获时钟精度指标。若要评估这些内容，需要人工标注、传感器信号或视频事件标注。

### 采集和数据治理

- 保留 `description`、`frame`、声明 dtype/shape、Arrow 实际存储类型、单位来源和 timestamp 前缀；当前声明为 float32、实际 Arrow 为 `list<double>` 的差异应进入数据契约检查，而不是静默转换。
- 采集时补充 episode 阶段边界、可见性/置信度、21 个点的名称和拓扑、MANO 模型版本、接触/力/深度信号及标定版本。没有这些材料时，报告应继续输出“未提供”。
- 为每个视频保存帧数、PTS、time base、关键帧结构和源对象摘要；对 20 个投影画外帧和两个较大运动转场保留可复核样本，交由数据生产方确认是否为真实运动、标注误差或标定边界。
- `success=true` 应保留来源 `meta/custom_metadata.csv` 和 `metadata_declaration` 状态；若要用于评估，需定义标签生成规则并增加多 episode 标签覆盖，不能把元数据声明升级成测量事实。

## 不能回答的问题

当前数据不能回答：总体成功率、泛化能力、子任务阶段边界、接触/力/触觉/深度、可见性和置信度、解剖点名称、MANO betas 的生成原因，以及没有独立真值时的像素对齐和漂移成因。需要这些结论时，应增加独立标注、传感器或采集流程证据，并记录采样、标定、滤波和时钟同步方式。

## 附录：可复核 ViewState URL

以下链接是对应的实际工具响应 URL，逐字复制保存；它们需要同一快照服务才能打开。

```text
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjB9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjp0cnVlLCJsb2NhbGUiOiJlbiJ9LCJlcGlzb2RlIjowLCJldmlkZW5jZSI6bnVsbCwib3ZlcmxheXMiOlt7InNpZGVzIjpbImxlZnQiLCJyaWdodCJdLCJ0eXBlIjoiaGFuZF90cmFja3MifV0sInBhbmVscyI6WyJ2aWRlbyIsInNlcmllcyIsIm1ldGFkYXRhIl0sInJlbmRlciI6eyJoZWlnaHQiOjc2OCwid2lkdGgiOjEwMjR9LCJzY2hlbWFfdmVyc2lvbiI6MSwic2VyaWVzIjpbImFjdGlvbi5sZWZ0X2hhbmRfdHJhY2tzIiwiYmFzZV8wX2NhbWVyYS9wb3NpdGlvbiJdLCJ0aW1lX3JhbmdlIjpudWxsfQ
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjB9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjp0cnVlLCJsb2NhbGUiOiJlbiJ9LCJlcGlzb2RlIjowLCJldmlkZW5jZSI6eyJjb21wb25lbnQiOm51bGwsImZpZWxkIjoidGltZXN0YW1wX25zIiwia2luZCI6InNlcmllc19xdWVyeSIsInBhZ2VfbGltaXQiOjMwMjksInBhZ2Vfb2Zmc2V0IjowLCJyZXF1ZXN0ZWRfZnJhbWVfcmFuZ2UiOnsiZW5kIjozMDI5LCJzdGFydCI6MH0sInJldHVybmVkX2ZyYW1lX3JhbmdlIjp7ImVuZCI6MzAyOSwic3RhcnQiOjB9fSwib3ZlcmxheXMiOlt7InNpZGVzIjpbImxlZnQiLCJyaWdodCJdLCJ0eXBlIjoiaGFuZF90cmFja3MifV0sInBhbmVscyI6WyJ2aWRlbyIsInNlcmllcyIsIm1ldGFkYXRhIl0sInJlbmRlciI6eyJoZWlnaHQiOjc2OCwid2lkdGgiOjEwMjR9LCJzY2hlbWFfdmVyc2lvbiI6MSwic2VyaWVzIjpbInRpbWVzdGFtcF9ucyJdLCJ0aW1lX3JhbmdlIjpudWxsfQ
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjB9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjp0cnVlLCJsb2NhbGUiOiJlbiJ9LCJlcGlzb2RlIjowLCJldmlkZW5jZSI6eyJjb21wb25lbnQiOjAsImZpZWxkIjoiYWN0aW9uLmxlZnRfaGFuZF90cmFja3MiLCJraW5kIjoic2VyaWVzX3F1ZXJ5IiwicGFnZV9saW1pdCI6MzAyOCwicGFnZV9vZmZzZXQiOjAsInJlcXVlc3RlZF9mcmFtZV9yYW5nZSI6eyJlbmQiOjMwMjgsInN0YXJ0IjowfSwicmV0dXJuZWRfZnJhbWVfcmFuZ2UiOnsiZW5kIjozMDI4LCJzdGFydCI6MH19LCJvdmVybGF5cyI6W3sic2lkZXMiOlsibGVmdCIsInJpZ2h0Il0sInR5cGUiOiJoYW5kX3RyYWNrcyJ9XSwicGFuZWxzIjpbInZpZGVvIiwic2VyaWVzIiwibWV0YWRhdGEiXSwicmVuZGVyIjp7ImhlaWdodCI6NzY4LCJ3aWR0aCI6MTAyNH0sInNjaGVtYV92ZXJzaW9uIjoxLCJzZXJpZXMiOlsiYWN0aW9uLmxlZnRfaGFuZF90cmFja3NbMF0iXSwidGltZV9yYW5nZSI6bnVsbH0
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjB9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjp0cnVlLCJsb2NhbGUiOiJlbiJ9LCJlcGlzb2RlIjowLCJldmlkZW5jZSI6eyJjb21wb25lbnQiOm51bGwsImZpZWxkIjoiYWN0aW9uLmxlZnRfaGFuZF90cmFja3MiLCJraW5kIjoic2VyaWVzX3F1ZXJ5IiwicGFnZV9saW1pdCI6MywicGFnZV9vZmZzZXQiOjAsInJlcXVlc3RlZF9mcmFtZV9yYW5nZSI6eyJlbmQiOjMsInN0YXJ0IjowfSwicmVxdWVzdGVkX3RpbWVfcmFuZ2UiOm51bGwsInJldHVybmVkX2ZyYW1lX3JhbmdlIjp7ImVuZCI6Mywic3RhcnQiOjB9LCJyZXR1cm5lZF90aW1lX3JhbmdlIjp7ImVuZCI6MTAwMTAwMDAwLCJzdGFydCI6MH19LCJvdmVybGF5cyI6W3sic2lkZXMiOlsibGVmdCIsInJpZ2h0Il0sInR5cGUiOiJoYW5kX3RyYWNrcyJ9XSwicGFuZWxzIjpbInZpZGVvIiwic2VyaWVzIiwibWV0YWRhdGEiXSwicmVuZGVyIjp7ImhlaWdodCI6NzY4LCJ3aWR0aCI6MTAyNH0sInNjaGVtYV92ZXJzaW9uIjoxLCJzZXJpZXMiOlsiYWN0aW9uLmxlZnRfaGFuZF90cmFja3MiXSwidGltZV9yYW5nZSI6bnVsbH0
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjF9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjp0cnVlLCJsb2NhbGUiOiJlbiJ9LCJlcGlzb2RlIjowLCJldmlkZW5jZSI6eyJjb21wb25lbnQiOm51bGwsImZpZWxkIjoibGVmdF9oYW5kL3RyYWNrcyIsImtpbmQiOiJzZXJpZXNfcXVlcnkiLCJwYWdlX2xpbWl0IjozLCJwYWdlX29mZnNldCI6MCwicmVxdWVzdGVkX2ZyYW1lX3JhbmdlIjp7ImVuZCI6NCwic3RhcnQiOjF9LCJyZXF1ZXN0ZWRfdGltZV9yYW5nZSI6bnVsbCwicmV0dXJuZWRfZnJhbWVfcmFuZ2UiOnsiZW5kIjo0LCJzdGFydCI6MX0sInJldHVybmVkX3RpbWVfcmFuZ2UiOnsiZW5kIjoxMzM0NjY2NjYsInN0YXJ0IjozMzM2NjY2Nn19LCJvdmVybGF5cyI6W3sic2lkZXMiOlsibGVmdCIsInJpZ2h0Il0sInR5cGUiOiJoYW5kX3RyYWNrcyJ9XSwicGFuZWxzIjpbInZpZGVvIiwic2VyaWVzIiwibWV0YWRhdGEiXSwicmVuZGVyIjp7ImhlaWdodCI6NzY4LCJ3aWR0aCI6MTAyNH0sInNjaGVtYV92ZXJzaW9uIjoxLCJzZXJpZXMiOlsibGVmdF9oYW5kL3RyYWNrcyJdLCJ0aW1lX3JhbmdlIjpudWxsfQ
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjE1MTN9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjp0cnVlLCJsb2NhbGUiOiJlbiJ9LCJlcGlzb2RlIjowLCJldmlkZW5jZSI6eyJjb21wb25lbnQiOm51bGwsImZpZWxkIjoiYWN0aW9uLmxlZnRfaGFuZF90cmFja3MiLCJraW5kIjoic2VyaWVzX3F1ZXJ5IiwicGFnZV9saW1pdCI6MywicGFnZV9vZmZzZXQiOjAsInJlcXVlc3RlZF9mcmFtZV9yYW5nZSI6eyJlbmQiOjE1MTYsInN0YXJ0IjoxNTEzfSwicmVxdWVzdGVkX3RpbWVfcmFuZ2UiOm51bGwsInJldHVybmVkX2ZyYW1lX3JhbmdlIjp7ImVuZCI6MTUxNiwic3RhcnQiOjE1MTN9LCJyZXR1cm5lZF90aW1lX3JhbmdlIjp7ImVuZCI6NTA1ODM4NjY2NjYsInN0YXJ0Ijo1MDQ4Mzc2NjY2Nn19LCJvdmVybGF5cyI6W3sic2lkZXMiOlsibGVmdCIsInJpZ2h0Il0sInR5cGUiOiJoYW5kX3RyYWNrcyJ9XSwicGFuZWxzIjpbInZpZGVvIiwic2VyaWVzIiwibWV0YWRhdGEiXSwicmVuZGVyIjp7ImhlaWdodCI6NzY4LCJ3aWR0aCI6MTAyNH0sInNjaGVtYV92ZXJzaW9uIjoxLCJzZXJpZXMiOlsiYWN0aW9uLmxlZnRfaGFuZF90cmFja3MiXSwidGltZV9yYW5nZSI6bnVsbH0
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjE1MTR9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjp0cnVlLCJsb2NhbGUiOiJlbiJ9LCJlcGlzb2RlIjowLCJldmlkZW5jZSI6eyJjb21wb25lbnQiOm51bGwsImZpZWxkIjoibGVmdF9oYW5kL3RyYWNrcyIsImtpbmQiOiJzZXJpZXNfcXVlcnkiLCJwYWdlX2xpbWl0IjozLCJwYWdlX29mZnNldCI6MCwicmVxdWVzdGVkX2ZyYW1lX3JhbmdlIjp7ImVuZCI6MTUxNywic3RhcnQiOjE1MTR9LCJyZXF1ZXN0ZWRfdGltZV9yYW5nZSI6bnVsbCwicmV0dXJuZWRfZnJhbWVfcmFuZ2UiOnsiZW5kIjoxNTE3LCJzdGFydCI6MTUxNH0sInJldHVybmVkX3RpbWVfcmFuZ2UiOnsiZW5kIjo1MDYxNzIzMzMzMywic3RhcnQiOjUwNTE3MTMzMzMzfX0sIm92ZXJsYXlzIjpbeyJzaWRlcyI6WyJsZWZ0IiwicmlnaHQiXSwidHlwZSI6ImhhbmRfdHJhY2tzIn1dLCJwYW5lbHMiOlsidmlkZW8iLCJzZXJpZXMiLCJtZXRhZGF0YSJdLCJyZW5kZXIiOnsiaGVpZ2h0Ijo3NjgsIndpZHRoIjoxMDI0fSwic2NoZW1hX3ZlcnNpb24iOjEsInNlcmllcyI6WyJsZWZ0X2hhbmQvdHJhY2tzIl0sInRpbWVfcmFuZ2UiOm51bGx9
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjMwMjV9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjp0cnVlLCJsb2NhbGUiOiJlbiJ9LCJlcGlzb2RlIjowLCJldmlkZW5jZSI6eyJjb21wb25lbnQiOm51bGwsImZpZWxkIjoiYWN0aW9uLmxlZnRfaGFuZF90cmFja3MiLCJraW5kIjoic2VyaWVzX3F1ZXJ5IiwicGFnZV9saW1pdCI6MywicGFnZV9vZmZzZXQiOjAsInJlcXVlc3RlZF9mcmFtZV9yYW5nZSI6eyJlbmQiOjMwMjgsInN0YXJ0IjozMDI1fSwicmVxdWVzdGVkX3RpbWVfcmFuZ2UiOm51bGwsInJldHVybmVkX2ZyYW1lX3JhbmdlIjp7ImVuZCI6MzAyOCwic3RhcnQiOjMwMjV9LCJyZXR1cm5lZF90aW1lX3JhbmdlIjp7ImVuZCI6MTAxMDM0MjY2NjY2LCJzdGFydCI6MTAwOTM0MTY2NjY2fX0sIm92ZXJsYXlzIjpbeyJzaWRlcyI6WyJsZWZ0IiwicmlnaHQiXSwidHlwZSI6ImhhbmRfdHJhY2tzIn1dLCJwYW5lbHMiOlsidmlkZW8iLCJzZXJpZXMiLCJtZXRhZGF0YSJdLCJyZW5kZXIiOnsiaGVpZ2h0Ijo3NjgsIndpZHRoIjoxMDI0fSwic2NoZW1hX3ZlcnNpb24iOjEsInNlcmllcyI6WyJhY3Rpb24ubGVmdF9oYW5kX3RyYWNrcyJdLCJ0aW1lX3JhbmdlIjpudWxsfQ
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjMwMjZ9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjp0cnVlLCJsb2NhbGUiOiJlbiJ9LCJlcGlzb2RlIjowLCJldmlkZW5jZSI6eyJjb21wb25lbnQiOm51bGwsImZpZWxkIjoibGVmdF9oYW5kL3RyYWNrcyIsImtpbmQiOiJzZXJpZXNfcXVlcnkiLCJwYWdlX2xpbWl0IjozLCJwYWdlX29mZnNldCI6MCwicmVxdWVzdGVkX2ZyYW1lX3JhbmdlIjp7ImVuZCI6MzAyOSwic3RhcnQiOjMwMjZ9LCJyZXF1ZXN0ZWRfdGltZV9yYW5nZSI6bnVsbCwicmV0dXJuZWRfZnJhbWVfcmFuZ2UiOnsiZW5kIjozMDI5LCJzdGFydCI6MzAyNn0sInJldHVybmVkX3RpbWVfcmFuZ2UiOnsiZW5kIjoxMDEwNjc2MzMzMzMsInN0YXJ0IjoxMDA5Njc1MzMzMzN9fSwib3ZlcmxheXMiOlt7InNpZGVzIjpbImxlZnQiLCJyaWdodCJdLCJ0eXBlIjoiaGFuZF90cmFja3MifV0sInBhbmVscyI6WyJ2aWRlbyIsInNlcmllcyIsIm1ldGFkYXRhIl0sInJlbmRlciI6eyJoZWlnaHQiOjc2OCwid2lkdGgiOjEwMjR9LCJzY2hlbWFfdmVyc2lvbiI6MSwic2VyaWVzIjpbImxlZnRfaGFuZC90cmFja3MiXSwidGltZV9yYW5nZSI6bnVsbH0
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjB9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjp0cnVlLCJsb2NhbGUiOiJlbiJ9LCJlcGlzb2RlIjowLCJldmlkZW5jZSI6eyJjb21wb25lbnQiOm51bGwsImZpZWxkIjoiYWN0aW9uLmxlZnRfaGFuZF90cmFja3MiLCJraW5kIjoic2VyaWVzX3F1ZXJ5IiwicGFnZV9saW1pdCI6MzAyOSwicGFnZV9vZmZzZXQiOjAsInJlcXVlc3RlZF9mcmFtZV9yYW5nZSI6eyJlbmQiOjMwMjksInN0YXJ0IjowfSwicmV0dXJuZWRfZnJhbWVfcmFuZ2UiOnsiZW5kIjozMDI5LCJzdGFydCI6MH19LCJvdmVybGF5cyI6W3sic2lkZXMiOlsibGVmdCIsInJpZ2h0Il0sInR5cGUiOiJoYW5kX3RyYWNrcyJ9XSwicGFuZWxzIjpbInZpZGVvIiwic2VyaWVzIiwibWV0YWRhdGEiXSwicmVuZGVyIjp7ImhlaWdodCI6NzY4LCJ3aWR0aCI6MTAyNH0sInNjaGVtYV92ZXJzaW9uIjoxLCJzZXJpZXMiOlsiYWN0aW9uLmxlZnRfaGFuZF90cmFja3MiXSwidGltZV9yYW5nZSI6bnVsbH0
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjB9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjp0cnVlLCJsb2NhbGUiOiJlbiJ9LCJlcGlzb2RlIjowLCJldmlkZW5jZSI6eyJhZ2dyZWdhdGlvbiI6ImNvdW50LG1pbixtYXgsbWVhbixwNTAscDk1IiwiY29tcG9uZW50IjowLCJleGFjdCI6dHJ1ZSwiZmllbGQiOiJhY3Rpb24ubGVmdF9oYW5kX3RyYWNrcyIsImlucHV0X2NvdW50IjozMDI4LCJraW5kIjoiYWdncmVnYXRlX3NlcmllcyIsInBhZ2VfbGltaXQiOjMwMjgsInBhZ2Vfb2Zmc2V0IjowLCJyZXF1ZXN0ZWRfZnJhbWVfcmFuZ2UiOnsiZW5kIjozMDI4LCJzdGFydCI6MH0sInJlcXVlc3RlZF90aW1lX3JhbmdlIjpudWxsLCJyZXR1cm5lZF9mcmFtZV9yYW5nZSI6eyJlbmQiOjMwMjgsInN0YXJ0IjowfSwicmV0dXJuZWRfdGltZV9yYW5nZSI6eyJlbmQiOjEwMTAzNDI2NjY2Niwic3RhcnQiOjB9fSwib3ZlcmxheXMiOlt7InNpZGVzIjpbImxlZnQiLCJyaWdodCJdLCJ0eXBlIjoiaGFuZF90cmFja3MifV0sInBhbmVscyI6WyJ2aWRlbyIsInNlcmllcyIsIm1ldGFkYXRhIl0sInJlbmRlciI6eyJoZWlnaHQiOjc2OCwid2lkdGgiOjEwMjR9LCJzY2hlbWFfdmVyc2lvbiI6MSwic2VyaWVzIjpbImFjdGlvbi5sZWZ0X2hhbmRfdHJhY2tzWzBdIl0sInRpbWVfcmFuZ2UiOm51bGx9
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjMwMjV9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjp0cnVlLCJsb2NhbGUiOiJlbiJ9LCJlcGlzb2RlIjowLCJldmlkZW5jZSI6eyJjb21wb25lbnQiOm51bGwsImZpZWxkIjoiYWN0aW9uLmxlZnRfaGFuZF90cmFja3MiLCJraW5kIjoic2VyaWVzX3F1ZXJ5IiwicGFnZV9saW1pdCI6MSwicGFnZV9vZmZzZXQiOjAsInJlcXVlc3RlZF9mcmFtZV9yYW5nZSI6eyJlbmQiOjMwMjYsInN0YXJ0IjozMDI1fSwicmVxdWVzdGVkX3RpbWVfcmFuZ2UiOm51bGwsInJldHVybmVkX2ZyYW1lX3JhbmdlIjp7ImVuZCI6MzAyNiwic3RhcnQiOjMwMjV9LCJyZXR1cm5lZF90aW1lX3JhbmdlIjp7ImVuZCI6MTAwOTY3NTMzMzMzLCJzdGFydCI6MTAwOTM0MTY2NjY2fX0sIm92ZXJsYXlzIjpbeyJzaWRlcyI6WyJsZWZ0IiwicmlnaHQiXSwidHlwZSI6ImhhbmRfdHJhY2tzIn1dLCJwYW5lbHMiOlsidmlkZW8iLCJzZXJpZXMiLCJtZXRhZGF0YSJdLCJyZW5kZXIiOnsiaGVpZ2h0Ijo3NjgsIndpZHRoIjoxMDI0fSwic2NoZW1hX3ZlcnNpb24iOjEsInNlcmllcyI6WyJhY3Rpb24ubGVmdF9oYW5kX3RyYWNrcyJdLCJ0aW1lX3JhbmdlIjpudWxsfQ
http://127.0.0.1:8000/view?state=eyJjdXJzb3IiOnsiZnJhbWUiOjMwMjV9LCJkaXNwbGF5Ijp7ImluZm9fYmFyIjpmYWxzZSwibG9jYWxlIjoiZW4ifSwiZXBpc29kZSI6MCwiZXZpZGVuY2UiOnsiY29tcG9uZW50IjpudWxsLCJmaWVsZCI6ImFjdGlvbi5sZWZ0X2hhbmRfdHJhY2tzIiwia2luZCI6InNlcmllc19xdWVyeSIsInBhZ2VfbGltaXQiOjEsInBhZ2Vfb2Zmc2V0IjowLCJyZXF1ZXN0ZWRfZnJhbWVfcmFuZ2UiOnsiZW5kIjozMDI2LCJzdGFydCI6MzAyNX0sInJlcXVlc3RlZF90aW1lX3JhbmdlIjpudWxsLCJyZXR1cm5lZF9mcmFtZV9yYW5nZSI6eyJlbmQiOjMwMjYsInN0YXJ0IjozMDI1fSwicmV0dXJuZWRfdGltZV9yYW5nZSI6eyJlbmQiOjEwMDk2NzUzMzMzMywic3RhcnQiOjEwMDkzNDE2NjY2Nn19LCJvdmVybGF5cyI6W3sic2lkZXMiOlsibGVmdCIsInJpZ2h0Il0sInR5cGUiOiJoYW5kX3RyYWNrcyJ9XSwicGFuZWxzIjpbInZpZGVvIiwic2VyaWVzIiwibWV0YWRhdGEiXSwicmVuZGVyIjp7ImhlaWdodCI6NzY4LCJ3aWR0aCI6MTAyNH0sInNjaGVtYV92ZXJzaW9uIjoxLCJzZXJpZXMiOlsiYWN0aW9uLmxlZnRfaGFuZF90cmFja3MiXSwidGltZV9yYW5nZSI6bnVsbH0
```