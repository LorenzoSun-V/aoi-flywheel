# 任务类型：检测、旋转框、实例分割、语义分割

平台目前只做**检测**（水平框，代号 `det`）。旋转框（`obb`）、实例分割（`seg`）、语义分割（`semseg`）的规则和接口在本文里先写好，按这里加，不重新设计。

- **现在可用：** `det`。
- **已写好、未开放：** `obb`、`seg`、`semseg`。任务表里有它们的一行（`enabled=False`），但数据库的 CHECK 只允许 `det`，建快照和新建模型也只接受 `det`。
- 标 **（暂定）** 的内容还没有定，到做那个任务时由负责人确认后再改本文。

## 1. 哪些东西有任务

任务在**建快照时写明**，不从标注猜（9 列的 YOLO 行既可能是旋转框，也可能是 4 点多边形）。

| 有任务 | 没有任务 |
|---|---|
| 训练集快照（`trainset.json` 的 `task`）、模型、模型版本、评估报告、预测集 | 数据池、标注批次、版本号、现场和产线、文件库 |

- 同一个数据池可以建检测快照，也可以建分割快照。
- 快照格式仍是 `fw-trainset 1`，靠 `trainset.json` 的 `task` 区分，不为新任务改格式名。
- `/api/v1` 里数据集版本的 `task`（API 级别 17）：快照是它的任务，其余是 `null`。网页在快照列表、快照页、概览的「数据池与快照」里标出任务。
- 标注批次没有任务：复核时的「改动统计」按形状类型比较，`rectangle` 对 `rectangle` 按 IoU 判断移动、改类别，其余形状按外接矩形比较（现在的做法，加任务时改成只按「完全相同、新增、删除」统计）。数据池版本之间的标注对比同样没有任务，按检测读成框比较。

## 2. 任务表

`client/fw/contract.py` 的 `TASKS`。一个任务的规则只写在这一处：服务端、`fw`、worker 都从这里取；网页不能 import Python，`server/fwserver/web/app.js` 里有一份同样内容的 `TASKS`，`test_the_web_pages_name_the_tasks_as_the_registry_does` 保证两份一致。

| 字段 | 含义 |
|---|---|
| `id`、`title` | 代号和网页上的名称：`det` 检测、`obb` 旋转框、`seg` 实例分割、`semseg` 语义分割 |
| `enabled` | 能不能选。只有 `det` 是 `True` |
| `shapes` | LiteAnno 的 `shape_type` 怎么读：`bbox`（取外接矩形）、`obb`、`polygon`；不在表里的拒绝 |
| `stored` | 快照里冻结哪几种标注：`yolo`、`coco`、`json`、`mask` |
| `pred_schema` | `predictions.json` 的 `schema_version`：检测是 1，其余是 2 |
| `key_metrics`、`main_metric` | 列表、对比里显示的指标，和概览上比较模型用的那一个 |
| `eval_tool` | 评估工具名，写进评估报告 |

一行 YOLO txt 的列数规则不在任务表里，写在这个任务的 `parse_txt` 里（见第 4 节）。

## 3. 各任务的数据规则

### 3.1 LiteAnno / X-AnyLabeling JSON 里接受的形状

| 形状 | det | obb | seg | semseg |
|---|---|---|---|---|
| `rectangle` | 框 | 角度为 0 的旋转框 | 4 点多边形 | 4 点多边形 |
| `rotation`（4 个角点） | 外接矩形 | 旋转框，以 4 个角点为准，不读 `direction` | 4 点多边形 | 4 点多边形 |
| `polygon`（至少 3 点） | 外接矩形 | 拒绝 | 多边形 | 多边形 |
| `point`、`line`、`linestrip`、`circle` 等 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |

不合规的直接拒绝，不自动换算（例如旋转框任务里出现 `polygon`，不取最小外接旋转矩形）。只做无损的换算：矩形当作角度为 0 的旋转框，矩形和旋转框当作 4 点多边形。拒绝时不建快照，报出有多少个文件、是哪种形状、怎么处理。

### 3.2 数据池里的 YOLO txt

| 任务 | 一行的格式（坐标均为 0 到 1） |
|---|---|
| det | `类别 cx cy w h`，5 列 |
| obb | `类别 x1 y1 x2 y2 x3 y3 x4 y4`，9 列 |
| seg | `类别 x1 y1 … xn yn`，至少 7 列且为奇数列 |
| semseg | 不接受 txt，只接受 JSON |

列数与快照的任务不符的，拒绝。

### 3.3 越出图片的形状

| 任务 | 规则 |
|---|---|
| det | 裁到图内；裁完没有面积的拒绝 |
| obb | 不裁。4 个角点原样保留并计数；中心点在图外的拒绝 |
| seg、semseg | 每个点的坐标夹到图内；夹完面积为 0 的拒绝 |

### 3.4 快照里冻结的文件

目录结构沿用 [训练集快照的格式](trainset-format.md)，只是标注内容随任务不同。`trainset.json` 的 `formats` 如实列出这个快照有哪几种。

| 任务 | `labels/<划分>/…txt` | `coco/<划分>.json` | `json/…json` | 其他 |
|---|---|---|---|---|
| det | YOLO 检测 | COCO 检测 | 原件 | |
| obb | YOLO-OBB：`类别 x1 y1 … x4 y4`，6 位小数，角点按文件里的顺序，不重排 | 不生成 | 原件 | |
| seg | YOLO-seg：`类别 x1 y1 … xn yn` | COCO，`segmentation` 为多边形，`bbox` 为外接矩形，`area` 为多边形面积 | 原件 | |
| semseg **（暂定）** | 不生成 | 不生成 | 原件 | `masks/<划分>/…/<原文件名>.png`：单通道 8 位索引图，0 是背景，类别表第 i 行（从 0 起）的像素值是 i+1，后写的形状盖住先写的，没有形状的是全 0；最多 254 类；用 Pillow 画，关闭抗锯齿，同一输入必须得到相同字节 |

### 3.5 `fw get` 摆出来的目录

- det、obb、seg 与 ultralytics 一致：平铺的 `train/images`、`train/labels`、`neg_train`、`val`、`test`、`data.yaml`。「没有目标的图」的判断方法不变（标注文件小于 9 字节）。
- semseg **（暂定）**：`train/images`、`train/masks`，val、test 同理；是否分 `neg_train`、掩码像素值的约定，等用过训练框架后再定。
- 划分按「有无目标」分层，规则不变。

## 4. 接口

### 4.1 标注读写（`server/fwserver/formats.py`）

每个任务实现同一组函数，`formats.of(task)` 取出这一组（`TaskFormat`）。`trainsets.py`、`evals.py` 只通过它读写标注，不直接调 `parse_json`、`yolo_bytes`。

| 函数 | 做什么 |
|---|---|
| `parse_json(text)`、`parse_txt(text, classes)` | 按 3.1、3.2 读一个标注文件，读不了时返回原因 |
| `refused` | 拒绝形状时报错里说的「只接受哪些形状」 |
| `fit(label, size)` | 3.3 的越界规则 |
| `yolo_bytes`、`coco_objects`、`coco_bytes`、`liteanno_bytes` | 写快照里冻结的标注；不生成 COCO 的任务不实现 |
| `mask_png` | 只有 semseg（暂定） |

现在只有 `formats.DET`。加任务时：`Label` 增加 `objects`（每个目标是类别名和像素坐标的点列表），检测已有的 `boxes`、`yolo_rows` 保留。`remap_json`、`remap_txt`、`label_classes`、`image_size` 与任务无关。

### 4.2 数据库与 `/api/v1`（加任务时做）

- 新迁移把 `trainset_versions.task`、`models.task` 的 CHECK 放宽一个值。每加一个任务放开一个值，没实现的不提前放开；不改已合并的迁移。
- 建快照、预览快照的请求体加 `task`，默认 `det`；网页建快照、新建模型的表单加「任务」选择；`fw trainset create --task`、`fw model create --task` 放开取值。
- 推理环境的 `framework` 对应一个任务（`jobs.FRAMEWORKS` 从元组改为字典）；提交评估、推理任务时，适配器的任务与模型的任务不一致的，拒绝。
- 产线基准测试集改为「每条产线每种任务一个」（同一条产线可能同时有两种任务的模型，各用各的测试集）：基准设置加任务字段，已有的行按 `det`，概览按任务分组比较。只有检测时不改。
- 每次 `API_LEVEL` 加一；`fw` 用到新参数时提 `MIN_SERVER_LEVEL`。

### 4.3 部署代码的适配器（`client/fw/trt_predict.py`）

用到的模型都有 C++ 部署代码和 pybind11 模块。每个适配器登记成 `名字: (任务, 工厂函数)`，工厂函数 `(engine 路径, conf) -> (infer, release)`。`infer(image)` 收 OpenCV 读出的 BGR 8 位图，坐标都是原图像素：

| 任务 | `infer` 的返回 |
|---|---|
| det | `[[x1, y1, x2, y2, 分数, 类别], …]` |
| obb | `[[x1, y1, x2, y2, x3, y3, x4, y4, 分数, 类别], …]`；部署代码给中心、宽高、角度的，在适配器里换成 4 个角点 |
| seg | `[{'box': [x1, y1, x2, y2], 'score': 分数, 'cls': 类别, 'mask': HxW 的 uint8 数组，或 'polygons': [[x, y, …], …]}, …]` |
| semseg **（暂定）** | HxW 的 uint8 数组，像素值与 3.4 的掩码约定相同 |

掩码转多边形在 `trt_predict.py` 里做（推理环境有 OpenCV）；`fw` 仍然只依赖 httpx。

### 4.4 预测集（`predictions.json`）

检测保持 schema 1。新任务用 schema 2：

```json
{"schema_version": 2, "task": "seg", "classes": ["…"], "conf": 0.1,
 "images": {"<路径>": {"size": [宽, 高], "objects": [ … ]}}}
```

| 任务 | `objects` 的每一项 |
|---|---|
| obb | `[x1, y1, x2, y2, x3, y3, x4, y4, 分数, 类别]` |
| seg | `{"box": [x1, y1, x2, y2], "score": 分数, "cls": 类别, "polygons": [[x, y, …], …]}` |
| semseg **（暂定）** | `{"cls": 类别, "polygons": [[x, y, …], …]}`，每类一项；另放 `masks/<序号>.png`，图片那一行加 `"mask"` |

- 服务端按 `task` 检查，与现在同样严格（每张图恰好一次、类别在类别表内、分数不低于 conf、坐标是有限数）。`prediction_sets.boxes` 的含义是「目标数」。
- 预标注按任务写 LiteAnno 形状：obb 写 `rotation`，seg、semseg 写 `polygon`；仍然 `checked=false`，仍然不自动写回成批次的新版本。

### 4.5 评估

流程不变。评估工具读「集合配置、预测文件」，写出 `summary.json`，每个集合有 `images` 和这个任务的指标。评估报告和任务结果里登记的指标名取自任务表的 `key_metrics`。

| 任务 | `key_metrics` | `main_metric` |
|---|---|---|
| det | mAP50、mAP50-95、P、R | mAP50-95 |
| obb **（暂定）** | mAP50、mAP50-95、P、R（IoU 按旋转框算） | mAP50-95 |
| seg **（暂定）** | mAP50(M)、mAP50-95(M)、P(M)、R(M) | mAP50-95(M) |
| semseg **（暂定）** | mIoU、mAcc | mIoU |

- aoi-deteval 目前只支持水平检测，其他任务以后在 aoi-deteval 里加；上表暂定的指标名以它实际写出的为准，到时改任务表和本表。
- 用数据池版本（非快照）做测试集时，标注按模型的任务用 3.1 的规则读，有不合规形状的拒绝评估。

## 5. 加一个任务的步骤

一个 PR 做一件事，顺序是旋转框、实例分割、语义分割，每个任务先数据侧再模型侧：

1. **数据侧：** 新迁移放开这个值；`formats.py` 实现这个任务的 `TaskFormat` 并登记进 `formats.of`；任务表这一行改 `enabled=True`（`app.js` 的副本同步）；建快照选任务（网页、命令行）；`fw get`；改 `docs/trainset-format.md`。
2. **模型侧：** 适配器、schema 2 预测集、预标注写对应形状、评估、网页按任务画预测和指标列。

加第一个新任务时，下面这些还按检测写着，要一并改成按任务：

- `predictions.py` 只收 `schema_version` 1，没有用到 `Task.pred_schema`。
- 网页新建模型写死 `task: 'det'`；概览的指标列取的是 `TASKS.det.key_metrics`，`main_metric` 还没有用到。
- worker 打印评估指标时用的是检测的指标名（`metrics_text` 的默认值）。
- 数据池版本对比（`changes.py`）和批次改动统计（`batches.py`）按检测读标注，见第 1 节。

不变的：`test_a_detection_snapshot_is_the_same_byte_for_byte` 固定了一组检测输入的快照清单哈希和每个文件的哈希，加任务后必须原样通过（[development.md](development.md) 规则表「检测快照的字节不因加任务而变」）。

## 6. 不做的

- 不加关键点（pose）、分类（cls）；需要时在任务表加一行。
- 不加 TensorRT 以外的推理方式，不加载 `.pt`、`.pth`。
- 不为旧数据加自动换算（例如多边形自动转旋转框）。
- 不改检测的任何格式和已有接口字段的含义。
