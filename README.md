# LeRobot Visualizer

这是一个同时面向人和 coding agent 的 LeRobot 3.0 数据 Visualizer。浏览器和 Agent 使用同一个版本化 ViewState 和同一个纯 Python renderer，因此同一个 ViewState 的浏览器画面和 headless PNG 一致。

## 环境和启动

需要 Python 3.12+。安装依赖：

```text
python -m pip install -r requirements.txt
```

复制 `.env.example`，然后设置以下两种数据源之一：

- `DATA_ROOT`：本地数据目录，保留 `meta/`、`data/` 和 `videos/` 的相对结构；
- `S3_BUCKET`、`S3_ENDPOINT`、`S3_REGION`：S3-compatible 只读数据源。凭据由 AWS 环境变量或标准 credential chain 提供。

外部浏览器 Origin 默认被拒绝；若确需外部访问，设置 `MCP_ALLOWED_ORIGINS` 为逗号分隔的完整 Origin 白名单。服务默认只监听 `127.0.0.1`。

一条命令启动：

```text
python -m app
```

Windows 也可以运行 `powershell -ExecutionPolicy Bypass -File scripts/start.ps1`。启动后访问：

- UI：`http://127.0.0.1:8000/view`
- ViewState 校验：`POST http://127.0.0.1:8000/api/view/normalize`
- MCP：`POST http://127.0.0.1:8000/mcp`
- 健康检查：`http://127.0.0.1:8000/health`

服务只执行对象列举、HEAD、GET 和 Range GET。缓存位于 `LEROBOT_CACHE_DIR`，不进入仓库；退出后可清理该运行时缓存目录，保留原始数据目录。

视频缓存 v2 使用数据源身份、完整对象路径及对象版本的摘要。本地版本采用流式 SHA-256；S3 优先使用 VersionId，其次 ETag，并执行版本/条件 GET。缓存需有匹配的长度和内容摘要记录，临时下载校验后才发布，校验失败不会回退到旧视频。

数据集在一次服务运行期间应保持不变。更换 `DATA_ROOT`、S3 配置或更新数据后，停止并重新启动服务；不支持运行期间热替换。首次检查版本和物化会增加读取工作，已校验的同一适配器不会逐帧重复哈希或下载视频。

## 架构和工具

`app/data` 负责本地/S3 只读适配和 LeRobot 元数据；`app/viewstate.py` 负责规范化和 URL；`app/renderer.py` 负责视频、时序曲线、metadata 和投影叠加；`app/tools.py` 是 Agent 工具实现；`app/main.py` 同时提供 UI、服务端 ViewState 校验、图片资源和 MCP Streamable HTTP JSON-RPC endpoint。

工具包括 `describe_dataset`、`list_episodes`、`query_series`、`aggregate_series`、`encode_view`、`decode_view`、`render_view`，以及新增的 `compare_series`、`audit_timing`、`audit_integrity`。详细调用规则、上限、时间语义和常见误用见 [`AGENTS.md`](AGENTS.md)。

查询 limit 超出 discovery 公布上限直接报错；合法查询因标量或完整响应字节限制缩页时，返回 requested_limit、returned_count、truncation_reasons。cursor 是原范围内偏移，续查保持原字段、component、范围和 limit。字段目录通过 field_cursor/next_field_cursor 独立分页。十个工具均支持 max_output_bytes（至少512）；业务错误返回 MCP isError 与稳定错误结构，协议错误仍为 JSON-RPC。

## ViewState 和无头渲染

ViewState 至少包含 episode、frame 或半开时间区间、面板、时序字段、overlay 和渲染尺寸。URL 只包含 canonical base64url JSON，不包含凭据，并限制为 `MAX_VIEW_URL_CHARS`（默认 16384）字符。查询证据会保留原始 field/component、请求和返回范围以及分页偏移。精确同步使用 `timestamp_ns` 和视频 PTS；float32 `timestamp` 只用于展示。数据采样域和视频容器域在 manifest 中分开暴露，越界时间范围会报错。

首页把空对象交给服务端规范化，默认选择 `base_0_camera/position` 和 `action.left_hand_tracks`；分享 URL 中显式的空 series 保持为空。字段控件同时提供整向量和 `field[index]`。整向量曲线显示数值均值，排除相机位姿、观测手部 tracks 中已知的内嵌时间戳；显式索引保留原始数据语义。

打开查询 URL 会保留 evidence。编辑 episode、frame、时间区间或 series 会清除旧 evidence；面板和 overlay 修改保留它。前端只在服务端规范化成功后更新 URL，失败时恢复最近有效的控件并隐藏旧图，提供重试和恢复默认视图按钮。晚到的旧响应不会覆盖最新状态。面板关闭后不绘制对应内容，时间范围和字段选择保留。

曲线窗口按 evidence、time_range、cursor 默认窗口的优先级解析。查询 evidence 使用当前返回页，聚合 evidence 使用完整输入范围，均不扩大；单行绘制单点。manifest 始终返回半开 `range_start_frame/range_end_frame` 和 `range_source`，关闭 series 面板也保留这些信息。普通时间区间的最小展示窗口扩大通过 `range_expanded` 标记。

`/api/view/normalize` 返回额外的顶层 `resolved_frame`，不改变 ViewState v1 或 URL。时间区间模式中的 frame 控件显示该帧；上一帧/下一帧从它步进，例如 1514 → 1513/1515，并清除 time_range 和旧 evidence。连续点击从待提交 cursor 累加；解析期间及 episode 首末边界禁用对应按钮。

浏览器 canvas、`/render` 与 MCP `render_view` 通过共享服务使用相同的合成和 PNG 编码核心。左右手世界坐标通过相机位姿和内参投影到 640×480 编码画布；画外点被裁剪并写入 manifest。首版只展示索引点，不假定解剖学名称或骨架拓扑。

字段目录保留原始 `description`、坐标系 `frame`、单位和 `units_source`。相机位置的 XYZ 为米、嵌入 timestamp 为 ns；MANO 是 camera 坐标系下的 61 个原始分量，按元数据分为 `3+45+10+3`，没有前置 timestamp，不给混合向量标统一空间单位。具体索引见 `AGENTS.md`。

正式播放器使用同一合成核心和 PNG。单帧播放接口返回 `LRF1` 有界封装：4 字节魔数、4 字节大端 JSON 长度、canonical ViewState/manifest/哈希元数据和 PNG。浏览器会对实际解码的 PNG 重新计算 SHA-256，再提交同一帧快照。服务级资源池由播放、`/render` 和 MCP 共享，所有会话、队列、压缩 PNG、像素和 dispatch 额度统一计数。

## 设计取舍和已知限制

- 结果有行数、标量数、完整 JSON 字节、URL 字符数和渲染尺寸上限；超限请求会拒绝或返回 `truncated` 和 `next_cursor`。完整 JSON 字节计算包含 values、timestamp_ns、证据和 view_url。
- 浏览器和 MCP 的 PNG 都受 `MAX_IMAGE_BYTES` 限制；浏览器失败会显示可操作错误，不保留旧画面作为成功结果。
- 当前数据只有一个 episode；不能据此估计成功率、泛化能力或任务阶段。
- 数据没有 21 点名称、骨架连线、置信度/可见性、深度、力或接触真值。
- 21 点投影能检查坐标系和大致对齐，但不等于像素级标定误差证明。
- 服务启动依赖 `requirements.txt` 中的 FastAPI、MCP 兼容 HTTP 运行时、PyArrow、PyAV、Pillow 和 NumPy。

## 我如何使用 AI 编码工具

本项目主要使用 Codex 辅助开发。我主要使用了四个职责不同的会话窗口中，组织“需求—实现—验证—修复”的迭代过程。

1. 第一个会话用于理解作业要求，制定整体模块规划，并审核开发会话提出的实施方案和阶段性交付结果。审核重点是需求是否覆盖、数据是否支持相关结论，以及验收方法能否证明功能正确。
2. 第二个会话承担具体实现。每个模块先在计划模式下明确输入输出、依赖关系、实现步骤和验收标准，再进行编码与验证。阶段性结果交由审核会话复核，被采纳的问题进入修复和复测流程。
3. 第三个会话从新的上下文接入已实现的工具层，依据工具描述与 `AGENTS.md` 尝试完成分析任务，用于检查未参与开发的 Agent 能否理解接口、选择合适工具、控制查询范围并提供可复核证据。
4. 第四个会话检查测试 Agent 的回答与调用过程，根据复核结果，区分代码问题、工具描述不足和 Agent 使用偏差，提出改进与补测方案。

我负责组织任务、确定范围与取舍，并结合实际执行结果判断是否接受修改。不同会话的审查意见用于发现问题。

## 如果再给我三天我会做什么

1、再额外进行一次完整的流程测试，并进行更大范围的压力测试，尝试更多可能超出边界的数据或行为，确保项目的可靠性与完整性。

2、补充一次 Agent 实验。使用新的 coding agent 会话，只提供工具描述和 `AGENTS.md`，尝试新增一个小型工具，验证当前Agent.md文档中该部分内容的有效性。

3、对网页端的功能和外观进行进一步的完善与美化；同时对工具层完成一遍完整的核对与验证。

