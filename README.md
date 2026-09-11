# 我会画给你看

此插件为“我会永远陪着你”插件的拓展程序，请到其拓展页进行配置。

0.4.5 提示词重写使用实际槽位 JSON 示例，兼容省略包装、唯一节点输入别名和空负面词。模型超时、限流或返回无效内容时，自动使用原提示词和已确认语义槽继续生成，并在生成记录和正式任务结果中保留降级状态。工作流映射无效、原始内容不足或用户取消时停止；不会再次调用模型或重复生图。

0.4.4 优化函数工具生图结果物化：统一校验 `data:image` 与 base64 图片签名，支持小型图片和带空格的相对路径，并继续在归档失败时保留明确的结果失败状态。

0.4.3 修复函数工具生图的 Windows 路径解析与会话传递。工具通过 `event.send` 提供的图片会在临时文件清理前归档，交由陪伴统一投递；已生成但解析或归档失败时停止生图后端回退。

0.4.2 修正原生 ComfyUI 运行状态与超时处理，统一结果归档清理；不完整直连配置在自动模式下继续允许其他后端回退，并加强提示词分析数据边界。

0.4.1 区分提示词模型限流、超时、鉴权与其他调用错误，保留明确失败阶段，避免将模型故障误报为 ComfyUI 连接失败。

0.4.0 内置 ComfyUI 连接、工作流节点识别、独立模型提示词重写和分槽、横竖画幅选择。补齐语义槽在新旧生成链路中的传递；不增加图片审查或自动重画。


0.3.8 改进自定义 `tool_call` 生图接入：透传真实会话、归档短生命周期图片，并兼容工具自行投递图片的返回协议。

0.3.7 新增 Anima 绘图大师（`astrbot_plugin_anima_master`）可选生图渠道，支持统一提示词、结果归档和实验性单图改图接入，并补充各类生图后端的安装与配置说明。

0.3.6 汇合生图可靠性回归测试，覆盖参考图身份与服装约束、提示词冲突清理、负面提示词、生成元数据和调试文件行为。

0.3.5 将 Image 运行层“自拍缺少身份参考图”从硬终止调整为提示词人物描述软降级，并记录 `identity_reference_fallback` 诊断事件；上层明确要求身份强一致性的业务仍可自行拦截。

0.3.4 修复新版 ImageTask v1 调用旧版生图执行器时协议元数据透传导致的 `unexpected keyword argument`，按旧执行器实际签名过滤兼容参数。继续保留 0.3.3 的正式 ImageTask v1 契约与旧式接口兼容能力。

0.3.3 补齐 ImageTask v1 正式联动契约（descriptor、任务构建/校验、参考图租约和执行结果），修复陪伴面板显示 `descriptor_method_missing` 导致生图扩展不可用的问题。保留旧式接口兼容，同时继续包含 SDGen 异步生成器、参考图库归属隔离、输出物料化和热加载状态容错修复。

0.3.0 新增结构化服装 V2：把居家服、睡衣、日常服等抽象类别展开为一套具体且内部一致的服装，结合时间、地点和体感温度，并区分官方服装、自由换装与真实穿搭参考图。自由换装支持角色/模型隔离的官方服装部件负面词；只有实际提交 outfit 参考图时才生成参考图服装约束。统一引擎路线还可通过 `outfit_mode_parameters` 调整已确认暴露的 LoRA 参数，未识别到参数槽时保持工作流原值。

`astrbot_plugin_image_companion` 是“我会永远陪着你”的图像生成扩展，提供统一的生成执行、后端管理和图片素材管理能力。

插件支持文生图、自拍与人像生成、参考图改图、参考图库、提示词与负面提示词处理，以及 ComfyUI、SDGen 和在线图片 API 等后端。它也负责生成任务的回退、状态记录和归档管理。

## ComfyUI 独立连接

在本插件配置的 **ComfyUI 独立连接与工作流适配** 中填写：

1. **ComfyUI 地址**：如 `http://127.0.0.1:8188`（同机部署示例；跨机器请填写生图机器地址）。这是生图机器地址，不是 AstrBot 面板地址。
2. **API 工作流文件**：每项填写 AstrBot 所在机器能够读取的 API 格式 JSON 文件路径。迁移时可直接引用旧插件的 API 工作流文件，仅作为文件来源，不需要加载中间插件。也可以将文件放进本扩展数据目录的 `comfyui_workflows` 文件夹，再直接使用文件名。
3. **默认文生图／自拍工作流**：填写完整文件名或列表中唯一的短名称，例如 `portrait`。
4. **提示词处理模型**：从已配置模型中选择。它专门处理工作流分析和提示词重写，可与聊天模型不同；生成时留空则复用陪伴的生图提示词模型。
5. **画幅选择**：`auto` 根据请求及重写模型建议选择；`workflow` 保留工作流尺寸；也可固定横图、竖图、方图。用户明确给出的尺寸优先，带参考图的自动模式默认保留工作流尺寸。为目标工作流设置合适的横竖尺寸、步长和像素上限。

生图后端选择 `comfyui`。填写直连地址后，默认旧链路也会直接使用内置服务，无需开启高级统一引擎；地址留空时维持原有中间插件兼容链路。这里的“独立”指不依赖 ComfyUI 中间插件，仍由陪伴主插件提供生成请求和最终投递。

管理员可以使用以下命令，无需启动额外网页或服务：

```text
画图工作流 连接
画图工作流 列表
画图工作流 识别 portrait
画图工作流 分析 portrait
```

`识别` 使用节点与连线规则，`分析` 额外调用所选模型理解各输入的含义；通过字段校验后保存填写规则，并返回映射结果。工作流改变后需要重新识别。多个输出或无法确定的输入不会被猜测为成功，可使用模型分析或手工映射明确选择。

重写模型不可用或输出无效时，会降级到原提示词和已有语义槽，保留工作流的固定设置与负面词，并在日志和生成记录中标注降级。明确指定的尺寸和横竖画幅仍生效；缺少模型画幅建议时沿用工作流尺寸。复杂工作流若无法用原始内容满足必要输入，则停止并说明原因。此降级只发生在提交前，不重试模型，也不对已提交的图片任务重新生成。

每次生成只调用一次提示词重写模型，按保存的规则填写标签、自然语言或多语义槽。已有编码器的非空文本默认追加，避免丢失固定提示词；明确的动态字符串入口使用替换。ANIMA 语义槽写入服装、姿态、背景和补充内容，保留工作流固定画师、质量词、模型与 LoRA。负面词默认追加到原工作流负面词。关闭重写时保留现有提示词及分槽规则。

工作流配置也支持 JSON 字符串对象（或配置文件中的对象），用于内嵌 API 工作流和手动填写规则：

```json
{
  "name": "portrait",
  "path": "/path/to/portrait-api.json",
  "mapping": {
    "fields": {
      "positive_prompt": {"node_id": "6", "input_name": "text", "kind": "prompt", "mode": "append", "description": "人物与画面描述", "format": "natural"},
      "negative_prompt": {"node_id": "7", "input_name": "text", "kind": "prompt", "mode": "append"},
      "width": {"node_id": "5", "input_name": "width", "kind": "number", "mode": "replace"},
      "height": {"node_id": "5", "input_name": "height", "kind": "number", "mode": "replace"}
    },
    "output_node": "9"
  }
}
```

示例节点编号仅演示格式，必须替换成实际工作流节点。`path` 也可替换成 `workflow` 节点字典。提示词支持 `append`、`replace`、`preserve`；不同阶段可使用不同槽名和描述。模型只填写已保存的文本槽，不修改工作流结构或任意采样参数。普通画布 JSON 暂不自动转换，请在 ComfyUI 导出 API 格式。

标准 `LoadImage` 输入会先上传图片再填写返回文件名，`ETN_LoadImageBase64` 使用 Base64；参考图仍来自现有陪伴／图片扩展素材链路。工作流没有图片输入时不会假装使用参考图。超时不会自动重提；只撤销尚在排队的本插件任务，已开始的任务可能仍在 ComfyUI 执行，不发送全局中断。

插件向宿主暴露 `test_comfyui_connection`、`import_comfyui_workflow`、`analyze_comfyui_workflow`，并沿用工作流列表、检查和映射校验接口。目前管理操作可通过上述命令完成，未增加陪伴面板专用按钮。

## 开发验证

使用 Python 3.12 或以上版本：

```text
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -q
```

测试沿用仓库的 AstrBot 边界替身，无需启动完整 AstrBot；HTTP 集成测试使用本机临时服务，不调用真实模型或 GPU。


## 与陪伴插件协同

与 [`astrbot_plugin_private_companion`](https://github.com/menglimi/astrbot_plugin_private_companion) 同时安装并启用后，陪伴插件会自动委托本插件执行图片生成。用户仍可使用原有的 `陪伴 生图`、`陪伴 自拍`、`陪伴 改图` 和自然语言入口；用户权限、主动额度、关系判断和最终消息投递仍由陪伴插件负责。

本插件不替换陪伴插件的人格、关系、主动调度或会话路由，也不提供独立页面或脱离陪伴主插件的运行入口。未检测到陪伴主插件时，扩展会保持不可用并等待宿主管理。

## 迁移

首次启用时，插件可读取 `astrbot_plugin_private_companion` 中遗留的生图配置、参考图库、生成目录和相关归档作为迁移来源。未在本插件中覆盖的配置会继续兼容旧设置，便于分阶段迁移。

迁移完成后，本插件负责实际生成执行、提示词处理、后端回退、参考图选择、生成轨迹和图片状态；陪伴插件继续负责对话意图、用户关系、主动策略、额度校验和最终发送。因此，原有会话中的图片投递位置和顺序不会改变。

## 配置

在陪伴插件“陪伴面板”的“生图”和“模型”页面可以管理：

- 在线图片 API、ComfyUI、SDGen 和自定义图片工具；
- 文生图与参考图工作流；
- 默认风格、固定提示词和负面提示词；
- 参考图库、生成归档和本地负载保护。

在本插件中明确填写的配置优先于旧插件设置；未覆盖的项目继续读取兼容来源。外部生图后端不可用时，本插件会按配置顺序尝试回退，并将失败原因记录到排障信息中。

## 各类生图接入方式

所有配置都在陪伴插件的“陪伴面板 → 生图/模型”页面完成。先安装并启用本插件和
`astrbot_plugin_private_companion`，再选择一种后端；本插件本身没有独立的生图命令入口。

### 方式选择

| 方式 | 适合场景 | 需要准备 | 参考图 |
| --- | --- | --- | --- |
| 在线图片 API | 不想维护本地 GPU，或使用云端模型 | 服务商 API 地址、Key、图片模型 ID | 取决于平台和模型 |
| ComfyUI | 本地工作流、提示词分槽、可控改图 | 可连接的 ComfyUI 服务与 API 工作流；也兼容旧中间插件 | 工作流包含有效图片输入时支持 |
| Anima 绘图大师 | 已在使用 YayiMiko/anima-master | 已加载的 `astrbot_plugin_anima_master`，在该插件中配置 ComfyUI 和模型 | 开启其 `img2img_enabled` 后支持单图重绘 |
| SDGen | 已经在使用 SDGen/Stable Diffusion WebUI | 已加载的 `astrbot_plugin_SDGen` 和可用 WebUI | 当前只按纯文生图接入 |
| 函数工具 | 已有其他插件提供生图函数 | 工具注册名和参数名 | 工具声明并接收参考图参数时支持 |
| 统一生图引擎 | 需要按模型档案、操作类型和回退路线管理 | `engine.mode=active` 及路线配置 | 由路线能力声明决定 |

### 在线图片 API

在“在线图片 API”中填写 `平台`、`API 地址`、`API Key`、`图片模型`。地址填写服务商的
API 根地址即可，插件会补齐生成或编辑路径；也接受直接填写完整路径。超时建议设置为
`180` 秒以上。结果可以是 URL 或 Base64，插件会自动下载/落盘并校验图片格式。

平台和能力如下（模型 ID 以服务商控制台为准）：

| 平台 | 平台值 | 地址/模型示例 | 参考图与协议 |
| --- | --- | --- | --- |
| OpenAI Images 兼容 | `openai` | `https://api.example.com/v1`；`gpt-image-1` | 文生图走 `/images/generations`；改图走 `/images/edits` multipart。能力取决于代理和模型 |
| OpenRouter | `openrouter` | `https://openrouter.ai/api/v1`；填写可生成图片的模型 ID | 文生图兼容 Images；改图使用 JSON `input_references`，参考图会转为 data URL |
| Agnes Image | `agnes` | `https://apihub.agnes-ai.com/v1`；如 `agnes-image-2.1-flash` | 支持文生图和参考图；可用 `ratio` 设置官方宽高比 |
| SenseNova 日日新 | `sensenova` | 使用 SenseNova 控制台的 API 根地址；`sensenova-u1-fast` | 当前按纯文生图使用，参考图请切换其他后端 |
| MiniMax | `minimax` | `https://api.minimaxi.com/v1` 或 `https://api.minimax.io/v1`；`image-01`/`image-01-live` | 统一调用 `/image_generation`；每次最多提交 1 张 PNG/JPEG 参考图 |
| 阿里云百炼 | `bailian` | `https://dashscope.aliyuncs.com/api/v1`；`qwen-image`、`wan` 系列 | Qwen/Wan 优先使用多模态 `input.messages`；带参考图时不会回退为纯文生图 |
| 魔搭社区 | `modelscope` | 使用魔搭 API 根地址；填写对应图片模型 | 使用异步任务和轮询；当前不接收参考图 |
| 豆包/火山方舟 | `doubao` | 使用 Ark API 根地址（通常 `/api/v3`）；`seedream`/`doubao-seedream` | 调用图片生成接口；当前不接收参考图 |
| Gemini | `gemini` | `https://generativelanguage.googleapis.com/v1beta`；支持图片输出的 Gemini 模型 | 调用 `models/{model}:generateContent`，支持文本和参考图；不适用于 Imagen 的 `:predict` 接口 |

`平台`填 `auto` 时会根据地址和模型名自动识别。Agnes、SenseNova 等兼容值也可以直接写入
端点配置，即使面板下拉列表未显示。模型必须是图片模型；把聊天模型填到这里通常会得到
“模型不支持 images 接口”的错误。

#### 多端点和回退

需要多家服务商或多个 Key 时，使用 `external_image_api_endpoints` 队列，按数组顺序尝试，
最多保留前 12 个端点。每项至少包含 `name`、`platform`、`base_url`、`api_key`、`model`，
还可填写 `size`、`ratio`、`timeout_seconds`、`enabled` 和 `custom_headers`。例如：

```json
[
  {
    "name": "主端点",
    "platform": "openai",
    "base_url": "https://api.example.com/v1",
    "api_key": "替换为你的 Key",
    "model": "gpt-image-1",
    "size": "1024x1024",
    "timeout_seconds": 180,
    "enabled": true
  },
  {
    "name": "备用端点",
    "platform": "gemini",
    "base_url": "https://generativelanguage.googleapis.com/v1beta",
    "api_key": "替换为你的 Key",
    "model": "gemini-2.5-flash-image",
    "enabled": true
  }
]
```

`custom_headers` 使用逐行的 `Header: value` 格式。普通端点队列会依次尝试启用的端点；
统一引擎的付费后备路线另由路线上的 `allow_paid_fallback` 控制，该字段不是普通端点队列的开关。

### ComfyUI 工作流（兼容旧中间插件）

新部署可优先使用上方的“ComfyUI 独立连接”。以下方式用于继续复用旧中间插件。

1. 安装并启用 AstrBot ComfyUI，确认 ComfyUI 服务可连接。
2. 在 ComfyUI 工作流管理中准备至少一个 `texts>=1、images=0、videos=0` 的文生图工作流。
3. 自拍或改图另准备 `images>=1` 的工作流；工作流的图片输入数量决定最多能接收几张参考图。
4. 在面板填写 `comfyui_text2img_workflow_name` 和 `comfyui_selfie_workflow_name`，名称必须与
     ComfyUI 中的工作流名称完全一致。

插件会自动读取工作流的文本/图片输入槽并轮询结果。带参考图时，如果找不到 `images>=1` 的
匹配工作流，会停止提交，避免静默退化成不使用参考图的纯文生图。统一引擎无法确定槽位时，
在 `engine.workflow_mappings` 中按工作流内容指纹提供经过验证的手动映射。

### SDGen / Stable Diffusion WebUI

安装并启用 `astrbot_plugin_SDGen`，让它连接到正在运行的 Stable Diffusion WebUI。插件会
自动发现注册名为 `SDGen` 或 `astrbot_plugin_sdgen` 的实例，并调用其 `_call_t2i_api`；无需
重复填写 WebUI 地址。SDGen 返回的第一张 `images` Base64 图片会被保存为 PNG，若启用了
SDGen 自身的放大处理也会沿用该设置。当前路线只用于纯文生图；自拍、改图或需要参考图时，
请使用 ComfyUI 或支持编辑接口的在线模型。

### Anima 绘图大师（anima-master）

已按 [YayiMiko/anima-master](https://github.com/YayiMiko/anima-master) **0.9.1** 的生成结果接口适配。
它和 AstrBot ComfyUI 是两个不同的插件，接入绘图大师不需要再安装 `astrbot_plugin_comfyui`。

1. 在 AstrBot 插件管理器安装上述仓库并启用，插件 ID 为 `astrbot_plugin_anima_master`。
2. 在**绘图大师自己的配置页**填写 `comfyui_base_url`、`unet_name`、`clip_name`、`vae_name`。
   模型名与 ComfyUI 下拉框一致；默认 `workflow=anima_t2i`，自定义工作流和千代模型/采样预设也由绘图大师管理。
3. 使用其 `/anm 状态` 确认服务及模型可用，然后在**陪伴面板 → 生图后端**选择
   **Anima 绘图大师（直连）**，配置值为 `anima_master`。不需要在线 API Key，也无需填写本插件的 ComfyUI 工作流名称。
4. 普通接入保持 `engine.mode=legacy`，提示词格式使用 `traditional`，再发送 `陪伴 生图 一片蓝天下的花田` 或 `陪伴 自拍`。

本插件配置片段：

```json
{
  "engine": {"mode": "legacy"},
  "image": {
    "photo_generation_backend": "anima_master",
    "photo_generation_prompt_format": "traditional"
  }
}
```

直连传递已整理的正面/负面提示词，复用绘图大师的默认宽高、采样步数、CFG、模型和工作流。
本次请求指定的 `宽x高` 会传给绘图大师，并由其 `allowed_sizes` 校验；未指定时沿用其默认尺寸。
提示词采用已准备模式，避免绘图大师再次优化时重置当前地点或服装；其提示词阶段的固定角色、
质量词和画师/画风预设不会再次拼接，需要的标签可填写在陪伴的固定附加提示词中。

**参考图与改图：**上游图生图仍是实验功能，默认关闭。需要时在绘图大师中开启
`img2img_enabled`，然后带图发送或引用图片使用 `陪伴 改图`。一次实际提交 1 张原图，
属于整图重绘，不能视为独立的身份/服装多图控制；重绘强度沿用 `edit_denoise`。
改图尺寸由原图和 `max_image_side` 决定，负面词沿用上游工作流默认值，诊断会记录这两项降级。
关闭该能力时，普通自拍可按人物描述生成，但缺少可提交原图的改图不会转成纯文生图。

直连只读取不发消息的生成结果，将 `outputs` 中的第一张图片复制到本插件归档后交给陪伴发送。
推荐使用专用的 `anima_master` 后端；使用 `tool_call + comfyui_generate` 时，工具通过传入的 `event.send` 提供图片，由图片扩展归档后交给陪伴发送，详见下方函数工具说明。
无需修改绘图大师的 `send_result_to_chat`，其独立 `/anm` 指令仍照常工作。
绘图大师的 `admin_only` 和 `allowed_sender_ids` 检查继续生效；陪伴桥接使用当前会话的合成事件，
它不冒充管理员，也不保证群聊合成事件包含原始请求者身份。

高级用户可将它加入统一路线表：

```json
{
  "image": {"photo_generation_backend": "anima_master"},
  "engine": {
    "mode": "active",
    "anima_scopes": ["text2img", "selfie", "portrait", "edit"],
    "routes": [
      {"name": "anima-text", "backend": "anima_master", "model_profile": "anima", "operation": "text2img", "timeout_seconds": 420},
      {"name": "anima-selfie", "backend": "anima_master", "model_profile": "anima", "operation": "selfie", "timeout_seconds": 420}
    ]
  }
}
```

每种操作单独建路线；`portrait`/`edit` 按同样结构添加，`edit` 仍要求上游图生图开关。
该后端的 `workflow` 留空，工作流始终由绘图大师配置，`workflow_mappings` 不作用于它。
`auto` 的原有后备顺序保持不变；需要 Anima 时显式选择此后端或配置统一引擎路线。

常见失败可按记录区分：未发现插件时检查加载状态与版本；`not_permitted` 检查绘图大师权限；
`comfyui_offline` 检查它的连接地址；`unsupported_size` 调整请求尺寸或 `allowed_sizes`；
“已完成但图片归档失败”表示已经出图，应检查磁盘与文件读取，不要直接重复提交。

### 自定义函数工具

先让其他 AstrBot 插件注册一个可调用的 LLM 工具，再在面板填写：

- `custom_photo_tool_name`：工具注册名；
- `custom_photo_tool_prompt_param`：提示词参数名，默认 `prompt`；
- `custom_photo_tool_kind_param`：可选的操作类型参数，会收到 `text2img`、`selfie`、`portrait` 或 `edit`；
- `custom_photo_tool_reference_param`：可选的参考图路径参数。

工具处理函数可接收 `(event, **kwargs)` 或 `(event=event, **kwargs)`。返回值需要包含图片路径、
图片 URL、data URL 或 Base64；JSON 对象支持 `image_path`、`path`、`file_path`、`image_url`、
`url`、`image_base64`、`base64`、`data` 等字段。选择了参考图却未配置参考图参数时，插件会
停止调用并提示配置，避免生成结果丢失人物或服装一致性。

工具如果通过 `event.send` 发送图片，可以继续使用 `tool_call`：传入事件保留原请求身份，图片扩展会接收并立即归档图片，交给陪伴统一发送。工具的独立命令不受影响。只返回图片路径、结构化 `outputs` 或 Base64 的工具仍由陪伴统一发送。

支持带中文和空格的 Windows 盘符／UNC 路径、POSIX 路径和 `file://` 图片地址。工具返回的“已发送”文字不作为投递回执。已经生成但结果解析或归档失败时会保留错误阶段，停止切换其他后端，避免重复生图。工具应使用传入的 `event.send` 或直接返回图片，不应绕过事件调用自己的平台发送接口。

### 统一生图引擎和 NAI

需要分操作启用 ANIMA、NAI 或其他模型档案时，在 `engine.routes` 中为每个操作建立路线：
`backend` 填 `comfyui`、`external` 或 `anima_master`，`model_profile` 填 `anima`、`nai`、`generic_natural`
或 `generic_tags`，`operation` 填 `text2img`、`selfie`、`portrait`、`edit`，`workflow` 填
工作流名称或在线端点名称。将 `engine.mode` 设为 `active` 才会真正提交；`shadow` 只编译和
诊断，`legacy` 使用旧链路。

NAI 路线只负责 NovelAI 标签和负面提示词编译。若使用 NovelAI 官方插件直连，保持官方直连
配置即可；只有 `active` 且存在有效 `model_profile=nai` 路线时，统一引擎才会接管，其他情况
不会重复提交。NAI 兼容代理应作为在线端点配置，并确认代理实际支持图片接口。

### 参考图能力速查

- 支持参考图：正确配置的 OpenAI 兼容编辑接口、OpenRouter、Agnes、MiniMax、Gemini，以及
  声明 `images>=1` 的 ComfyUI 工作流。
- 当前纯文生图：SenseNova U1 Fast、SDGen、魔搭社区、豆包/火山方舟。
- 百炼的 Qwen/Wan 图片模型使用多模态协议；带参考图时必须保证模型和端点支持该协议。
- Anima 绘图大师在开启实验性 `img2img_enabled` 后支持单张原图重绘。
- 参考图库只负责提供素材，最终能提交几张图由所选后端的能力上限决定；超出上限时会保留主
  参考图并把其余职责转成提示词。

## 统一生图引擎

0.2 引入后端与模型相互隔离的统一引擎。ComfyUI 是执行平台，ANIMA、NAI、自然语言在线模型和通用标签模型是独立模型档案；每次切换后端或后备路线都会针对目标模型重新编译提示词，并重新规划可提交的参考图。

配置中的运行模式：

- `legacy`：只走原有稳定链路；
- `shadow`：构建结构化场景、服装、参考图和模型提示词，但不向新后端提交；
- `active`：按路线表执行；
- “立即回滚到旧链路”：无论当前模式为何都停用新引擎。

ANIMA 可按文生图、自拍、人像或改图范围逐步开启。ComfyUI 工作流由内容指纹和命名槽识别，不固定为某一份工作流；自动识别不确定时必须提供通过验证的手动映射。在线 API 端点也各自拥有模型档案、能力清单、参考图上限和付费回退保护。

NAI 档案负责 NovelAI 标签提示词和独立负面提示词编译，可绑定到用户配置的 NovelAI 兼容端点或代理；插件不会凭空创建官方账户、令牌或收费调用，也不把 NAI 语法投射到 ANIMA 路线。

新版陪伴插件同时支持官方 NAI 生图插件直连。默认仍尊重这条官方直连；只有统一引擎处于 `active`、路线配置有效且当前操作存在 NAI 模型路线时，本插件才通过公共能力握手接管该请求。`shadow`、立即回滚、无匹配操作或无效路线均不会截断官方直连，从而避免双重提交和重复计费。

参考图库和远程参考图缓存由本插件独立持久化。统一 ComfyUI 适配器只自动信任本插件自己的 `photo_reference_images` 缓存目录；其他本地目录仍须在“允许读取参考图的目录”中显式授权。

场景策略会读取陪伴插件提供的当前日程、地点、睡眠阶段、当前温度和体感温度。明确服装要求优先，其次是对话连续服装、睡前/居家/活动/温度策略，最后才是每日穿搭和参考图默认服装。炎热环境会明确排除毛衣、厚针织、卫衣和厚外套。
