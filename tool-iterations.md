# LeRobot Visualizer 工具与 Agent 分析迭代记录

## 1. 执行记录和预算

**存在问题**

- 修改前 Agent 记录只有会话导出，明确缺少完整 MCP 响应载荷，部分计算代码和逐页账本无法定位。
- 旧记录中的预算按字符自述，不能复核 UTF-8 字节；本地计算失败后曾出现重复查询。
- 首次实验还发生过工具隔离/审批配置失败，若只保留最后成功答案会掩盖真实过程。

**如何修复**

- 每次业务调用先保存原始请求和响应，再进行解析和计算。
- 以所有业务 `content.text` 的 UTF-8 字节计入主预算；错误、重复和丢弃结果也计入。协议发现、完整 MCP 对象、传输和 PNG 单独统计。
- 使用唯一 run 目录保存提示词、指南、工具 schema、源码哈希、客户端代码、回答、图片和校验结果；失败、续接和勘误不删除。

**修复后的效果**

- 实际完成 **21/40 次业务调用、48,389/204,800 UTF-8 字节**，离线校验通过；交付了 2 张实际 PNG，模型收到的 PNG 与业务响应逐字节一致。
- 首次拒绝的实际文本为 61 字节；Agent 自述 71、续接提示误写 58，原始内容和勘误均保留，账本按 61 计数。

## 2. 分页、边界和错误恢复

**存在问题**

- 修改前服务的参数 schema 与运行上限不一致，例如曾请求 `limit=512` 但只返回 256 行。
- 本页范围、原始请求范围和 cursor 含义容易混淆；非零起点续查可能被误当作绝对帧号。
- 旧错误返回和完整响应大小没有统一计入，无法稳定判断是否截断。

**如何修复**

- discovery 的上限从运行配置生成；超过上限直接返回有界业务错误。
- 合法请求因行数、标量数或完整 UTF-8 文本上限缩页时，返回 `requested_limit`、`returned_count`、`truncation_reasons` 和 `next_cursor`。
- 明确 `start_frame/end_frame` 是本页范围，`requested_frame_range` 是原始范围，cursor 是原范围内偏移；续查只复用原始参数并替换服务返回的 cursor。

**修复后的效果**

- `[1513,1518)`、`limit=2` 稳定得到 `[1513,1515)`、`[1515,1517)`、`[1517,1518)`，没有重复或遗漏。
- 越限、混用 frame/time range、错误 cursor 和无法容纳单行的情况均有结构化错误，不再静默改写用户选择。
- 真实 HTTP 复核共 16 次、62,485 UTF-8 字节，独立于 Agent 主预算。

## 3. ViewState、证据 URL 和渲染范围

**存在问题**

- 查询 URL 曾丢失 component、原始/返回范围和分页位置；手工转录还出现过宽度 2024 的损坏 URL。
- 查询结果的 evidence 保存了范围，但渲染器未必按返回页展示，导致证据 URL 与画面不一致。
- 单变量实验只比较临时状态，不能证明最终交付文件中的 URL 仍然等价。

**如何修复**

- ViewState v1 增加完整 evidence：字段、component、请求/返回 frame/time range、分页 offset、聚合输入范围和结果范围。
- 统一由服务端生成、规范化和解码 canonical URL；查询 evidence 优先使用返回页，聚合 evidence 使用完整输入范围，时间范围和普通 cursor 作为后备。
- 单变量实验深拷贝完整 canonical state，只修改一个字段；最终文件中的 URL 再次解码并与来源调用核对。

**修复后的效果**

- frame 3025 的单变量对照中，字段、范围、component、尺寸和 overlays 均保持不变，只有 `display.info_bar` 改变。
- query、aggregate、浏览器 `/render` 和 MCP `render_view` 使用同一 canonical 状态和渲染范围；PNG 字节与 SHA-256 可交叉复核。
- 两条最终 Agent URL 均通过原始调用、解码状态和范围校验。

## 4. 字段语义、元数据来源和时间规则

**存在问题**

- 旧目录容易把声明 dtype/shape、首行观测长度和全量结构混为一谈。
- 原始 `description`、坐标系、timestamp 前缀、MANO 分段和单位来源曾无法完整传给 Agent。
- `success` 来源、数据时间域和视频时间域容易被误解为工具测量结果或同一个范围。

**如何修复**

- 字段目录同时保留原始描述、声明 dtype/shape、Arrow `storage_dtype`、`frame`、timestamp 前缀、component layout、单位来源和观测 scope；未知语义不猜测。
- episode 元数据按白名单读取，`success` 带来源和状态；数据域使用 `[data_start_ns,data_end_ns)`，视频域单独记录。
- 指南明确 MANO 的 camera 坐标系和 `[0,3)`、`[3,48)`、`[48,58)`、`[58,61)` 原始分段，不添加未声明的关节名称或统一单位。

**修复后的效果**

- 当前快照由工具确认：1 个 episode、3029 帧、16 个字段、1 个视频，任务原文为 `Sew fabric pieces`。
- `success=true` 明确来自 `meta/custom_metadata.csv:episode_index=0:success`，状态是 metadata declaration，未扩展为成功率。
- Agent 能区分 camera 位置的 timestamp 与 XYZ、带 timestamp 的 observed tracks、无前缀的 action/MANO，并使用 `timestamp_ns` 进行精确同步。

## 5. 新增诊断工具和分析边界

**存在问题**

- 仅靠原始分页传回数组，难以完成大范围配对、时间和完整性检查。
- 普通严格读取遇到坏数据时，无法区分“确实没有异常”和“尚未检查”。
- 旧分析容易把单分量结果扩展到全部 63 维、把全零向量解释成静止或终止，或把投影计数解释成像素对齐。

**如何修复**

- 新增有界 `compare_series`、`audit_timing`、`audit_integrity`，统一受行数、标量数、字段数、例子数、长度桶和工作内存限制。
- 诊断路径在严格拒载前仅读取选定列和批次；返回完成范围、剩余范围、截断状态和 continuation。
- 指南固定事实/推断/假设边界，明确未来 observation 不能作为 t 时刻输入，全零不证明成功/终止，投影分类不证明可见性或像素校准。

**修复后的效果**

- `compare_series`：`action.left_hand_tracks` raw0[t] 对 `left_hand/tracks` raw1[t+1]，范围 `[0,3028)`，3028 个有限配对全部精确匹配；结论只适用于该字段和分量。
- `audit_timing`：检查 3029 个 timestamp、3028 个间隔、3029 个 PTS，公开 `time_base=1/979001` 和整数换算规则；不据此声称捕获时钟精度或漂移成因。
- `audit_integrity`：左 action 全 3029 行中 1 个精确全零向量，无 null/非有限；全零与均值为零保持区分。
