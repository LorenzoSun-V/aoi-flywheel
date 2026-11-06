# 训练集快照的格式

训练集快照（`fw trainset create` 建的）是一个**数据集版本**，数据集的类型是「训练集快照」。所以它和别的版本一样冻结、按清单核对，用 `fw get`、`fw export`、`fw verify`、`fw dataset diff` 取回、导出、核对和对比。清单的哈希就是这个快照内容的标识。

快照冻结的是**划分的结果**，不是规则：每张图在 train、val、test 里的哪一个，类别编号对应哪个名称。取回时按快照原样摆出来，永远不重新划分，换代码、换种子都不会变。

本文是格式约定：训练代码按这里写的读，平台以后改动时要保持兼容（见[格式和接口的版本](development.md#格式和接口的版本)）。快照用 `fw get` 按 YOLO（默认）、COCO 或 LiteAnno JSON 的目录摆出来，见下文。

## 快照里存的文件

```
classes.txt                                   类别表：一行一个类别名，行号（从 0 起）就是类别编号
trainset.json                                 这个快照怎么来的：来源、类别、划分规则、每类框数、格式
images/<划分>/<现场>/<产线>/<原路径>            每张图片
labels/<划分>/<现场>/<产线>/<原文件名>.txt      YOLO 检测标注
json/<划分>/<现场>/<产线>/<原文件名>.json       数据池里的 LiteAnno / X-AnyLabeling 标注，原样（类别映射改到了的是改过的）；YOLO txt 标注的图是从框生成的
coco/<划分>.json                              这个划分的 COCO 检测标注
```

- `<划分>` 是 `train`、`val`、`test`；`<原路径>` 是这张图在来源数据池版本里的路径，原样保留。快照里存的（也就是 `--format all` 取回的）按 `<现场>/<产线>/` 分目录；`fw get` 按格式摆出来时去掉这两层，见下文。
- 三种标注都是**建快照时在服务器上一次转好、和图片一起冻结的**。以后改了转换代码，已有快照的文件也不会变；训练用的就是冻结的那一份。
- 同一张图在文件库里只存一份，快照不多占图片的空间；多出来的只有转换出来的标注文件。

## fw get 按格式摆出来

`fw get <快照> --to <目录> --format <格式>`，默认 `yolo`。只改路径，内容就是冻结的文件，每个都按清单核对过。三种格式都带 `classes.txt` 和 `trainset.json`。

**取回的路径比存的短**（0.2.3 起建的快照，`rule` 里有 `"paths": "short"`）：`<原路径>` 前的 `<现场>/<产线>/` 去掉，原路径里**最后一个**叫 `images` 的目录也去掉（数据池按 YOLO 放的 `images/` 层，它的 `labels/` 建快照时已经配好了）。例如数据池 `tx_v8` 的 `images/1.bmp` 取回成 `images/train/1.bmp`、`labels/train/1.txt`，`b1/images/x/2.png` 取回成 `images/train/b1/x/2.png`。去掉后有两张图路径相同（几个来源里有同路径的图，或者一个池里同时有 `images/a.png` 和 `a.png`）时不建快照，预览列出重名的路径。0.2.3 以前建的快照取回时仍是 `<现场>/<产线>/<原路径>`。

| 格式 | 摆出来的目录 | 给谁用 |
|---|---|---|
| `yolo`（默认） | `images/<划分>/…`、`labels/<划分>/….txt`，另生成 `data.yaml` | ultralytics：`yolo train data=<目录>/data.yaml`。它把图片路径里**最后一个** `images/` 换成 `labels/` 找标注（aoi-deteval 也一样），所以原路径里叫 `images` 的目录在这个格式里改名成 `images_`（原来就叫 `images_`、`images__` 的再多加一个 `_`），例如原路径 `a/images/batch/images/1.png` 取回成 `images/val/a/images_/batch/1.png` 和 `labels/val/a/images_/batch/1.txt`；别的格式不改名 |
| `coco` | `train/`、`valid/`、`test/` 下放图片，每个目录里一个 `_annotations.coco.json` | RF-DETR 等读 Roboflow 式 COCO 目录的训练代码；别的 COCO 代码分别指图片目录和标注文件即可 |
| `json` | `train/`、`val/`、`test/` 下图片和同名 `.json` 放在一起 | 用 LiteAnno 或 X-AnyLabeling 打开看图、核对标注 |
| `all` | 和快照里存的一样 | `fw export` 导出时用的就是这个 |

- `data.yaml` 是取回后按 `classes.txt` 生成的，**不在快照里**：`path` 是取回目录的绝对路径，`train` / `val` / `test` 只写快照里有的划分，`names` 是类别名。目录挪了位置，改一下 `path` 或重新取回即可。
- 快照没有 train 或 val 时（例如只有几张图，或者整版都是测试集），`data.yaml` 照样生成，但 ultralytics 训练要 train 和 val 都有：建快照的预览和 `fw get` 都会说缺哪个，`fw.get_trainset()` 返回的 `missing_splits` 列出来。
- 某种格式没有生成时（见下文），取那种格式会报错并说明原因。

## 标注怎么配对

每张图找它的标注，两个地方都找：

1. **同目录同名**：`2026-09/0001.png` 配 `2026-09/0001.json` 或 `2026-09/0001.txt`；
2. **YOLO 找标注的位置**（只认 `.txt`）：把图片路径里**最后一个** `images/` 换成 `labels/`，例如 `batch1/images/0001.png` 配 `batch1/labels/0001.txt`，和 ultralytics、aoi-deteval 找标注的方式一样。

- 一张图既配到 JSON 又配到 txt（比如 `images/` 里有 `0001.json`，`labels/` 里有 `0001.txt`），要用 `--label-source json` 或 `--label-source txt` 说用哪个，没说就不建。只有一种的图照样用自己那一种。
- 同一种配到两个（同目录一个 `0001.txt`、`labels/` 下又一个）不建：不知道用哪个。给了 `--label-source` 也一样，选另一种不会把这个问题盖过去。
- 一个标注同时配到两张图也不建：例如 `batch/labels/0001.txt` 旁边有一张 `batch/labels/0001.png`，它又是 `batch/images/0001.png` 找标注的位置。
- 没有图认领的标注（孤立标注）不建，多半是放错了目录。
- 没有标注的图是负样本，照样收进来（`--require-labels` 时不建）。

## 类别映射

类别表属于快照，不属于数据池：数据池里的 `classes.txt` 不读，类别表是建快照时用 `--classes` 给的。标注里的类别和这张表对不上时，有两个选项，**只改快照里的标注，数据池里的不变**：

- `--label-classes <类别表>`：txt 标注自己的类别表（行号是 txt 里的类别编号）。不写就按 `--classes` 读。例如数据池的 txt 按 `Dirty Scratch Crack Solder others` 5 类编号，快照只要前 4 类：`--label-classes classes5.txt`。
- `--map 源=目标`（可写多次）：标注里的「源」类别算作类别表里的「目标」，例如 `--map DL1=DL --map DL2=DL` 把两类合成一类；**目标留空就去掉这类框，当背景**，例如 `--map others=`。目标必须在类别表里。

规则：

- 映射在核对类别之前做，按类别名、只做一次（`A=B` 和 `B=` 同时写时，A 的框算作 B，不会再被去掉）。映射之后还有类别表以外的类别就不建。
- JSON 标注：改形状的 `label`，删掉映射成空的形状（不管是什么形状），文件里别的内容（其余形状、`flags`、`description`、自定义的键和它们的顺序）不动；数字按原文写回（`0.1234567890123456789`、`1e1`、`-0`、`NaN` 都原样，不经过数值转换）；字符串里没法用 UTF-8 写的孤立代理码位（例如 `\udc80`）按转义写回。按 `indent=2`、UTF-8 写回，最后有换行。
- txt 标注：去掉映射成空的行，按快照的类别表重新编号；每行的四个坐标**原样保留**，只换第一个数（数之间统一成一个空格，换行统一成 LF），重写不会让框变形，和同一快照里的 COCO 一致。行没去掉、编号也没变的文件原样冻结。
- 框全被去掉的图是**没有框的背景图**：快照里有它的空标注文件，`counts` 里不算 `negatives`（那是没有标注文件的图）。
- 映射的源类别在哪个标注里都没有，就不建（多半是写错了，类别名区分大小写）。
- 预览里列出每条映射去掉或并进了多少个框、多少张图的框全被去掉；用的 `--label-source`、`--label-classes`、`--map` 记在 `rule` 里，`fw trainset show` 也显示。

## 标注怎么转

这一版只做**检测**（`trainset.json` 里 `task` 是 `det`）：每个目标是一个和坐标轴对齐的框。

LiteAnno / X-AnyLabeling JSON：

- `rectangle`、`polygon`、`rotation` 都按所有点的外接矩形当检测框（和 aoi-deteval 读标注的方式一样）。没有 `shape_type` 或为 `null` 的按 `polygon`。不是矩形的个数在预览里列出来。
- `point`、`line`、`linestrip`、`circle` 等不能当检测框，有就不建快照。
- 图片尺寸用标注里的 `imageWidth`、`imageHeight`；没有时读图片的文件头（PNG、JPEG、BMP、GIF、TIFF、WebP），也读不出来就不建。
- 超出图片的框裁到图片内，个数在预览里列出来；完全在图片外的框不建快照。

数据池里本来就是 YOLO txt 的，原样放进 `labels/`（文件开头有 UTF-8 BOM 的去掉 BOM，别的字节不变；类别映射或 `--label-classes` 改到了的只换类别编号、去掉映射掉的行，坐标原样）：每行必须是 `类别 中心x 中心y 宽 高` 5 个数，坐标在 0 到 1 之间（分割、旋转框、关键点格式的行不建快照）。

**YOLO**（`labels/…/*.txt`）：每个框一行 `类别编号 中心x 中心y 宽 高`，按图片宽高归一化，保留 6 位小数，换行是 LF。标注里一个框也没有的图片是空文件；没有标注文件的图片（负样本）没有 txt，ultralytics 都当作背景图。

**COCO**（`coco/<划分>.json`）：

- `images` 按样本标识排序，`id` 从 1 起，`file_name` 是相对这个划分图片目录的路径，也就是 `fw get --format coco` 取回的路径（0.2.3 起是上文的短路径，以前是 `<现场>/<产线>/<原路径>`），`width`、`height` 是图片尺寸；负样本也在里面，只是没有标注。
- `annotations` 的 `bbox` 是像素坐标 `[x, y, 宽, 高]`，`area` 是宽乘高，`iscrowd` 是 0，`segmentation` 是空列表。
- `categories` 的 `id` **就是 classes.txt 的行号，从 0 起**，和 YOLO 的类别编号一致，没有为背景留 0 号。
- 要知道每张图的尺寸：没有 JSON 标注的图（负样本、YOLO txt 标注的）读文件头。有图读不出尺寸时不生成 COCO，其余照常。

**JSON**（`json/…/*.json`）：数据池里有 JSON 的，就是原文件（类别映射改到了的，是改过的那份）。数据池里是 YOLO txt 的，建快照时从框生成一个，给 LiteAnno 打开看图用：每个框一个 `rectangle`（像素坐标，和 COCO 一样裁到图内，保留 4 位小数），`label` 是类别名，`imagePath` 是图片文件名，`imageWidth` / `imageHeight` 从图片文件头读，`version` 是 `"3.1"`。有 YOLO txt 标注的图读不出尺寸时，`json` 格式不生成。

`trainset.json` 的 `formats` 列出这个快照有哪些格式，`missing_formats` 写着没生成的格式和原因。

图片的 EXIF 旋转不处理：工业相机和监控相机的图基本不带 EXIF 方向。

## 样本的标识

每个样本的标识是 `<现场>/<产线>/<原路径>`（图片的路径），例如 `SES/TX/2026-09/0001.png`。同一张图在不同快照里标识不变，接着上一版划分、划分清单、换了划分的统计都按它对。标识就是 `--format all` 取回的 `images/<划分>/` 下的路径；`fw get` 按格式摆出来的路径更短，不是标识。

## classes.txt

UTF-8，一行一个类别名，最后有换行。第 1 行是编号 0。建快照时给的类别表原样写进来，这个快照里所有标注都已核对过（类别映射之后）：JSON 标注里每个形状的 `label` 都在类别表里，YOLO txt 每行第一个数在 0 到类别数减 1 之间。YOLO 和 COCO 的类别编号都是这里的行号。

## trainset.json

UTF-8 JSON，键的含义：

| 键 | 含义 |
|---|---|
| `format` | 固定是 `fw-trainset 1`；以后格式不兼容地改动时才变 |
| `name` | 快照名称 |
| `task` | `det`（检测） |
| `formats` | 这个快照有的标注格式：`yolo`、`coco`、`json` |
| `missing_formats` | 没生成的格式和原因，例如 `{"coco": "3 张图读不出尺寸……"}` |
| `sources` | 来源数据池版本，每项 `ref`（例如 `SES-TX@4`）、`manifest_sha256`（那个版本的清单哈希）、`site`、`line` |
| `based_on` | 接着哪个快照的划分，例如 `SES-TX-det@1`；第一版、重新划分、用划分清单时是 `null` |
| `classes` | 类别名列表，和 classes.txt 一样 |
| `rule` | 划分规则和读标注的规则（只是记录给人看，取回时不再用），见下表 |
| `counts` | `train`、`val`、`test` 各自的 `samples`（样本数）、`negatives`（无标注的）、`boxes`（每个类别的框数，`.json` 每个形状算一个，`.txt` 每行算一个） |

`rule` 的键：

| 键 | 含义 |
|---|---|
| `mode` | `fresh` 重新划分，`continue` 接着上一版，`split-file` 用划分清单 |
| `val` | train/val 里 val 的比例 |
| `seed` | 种子 |
| `test_dirs` | 新样本进 test 的目录 |
| `merge_test` | 是否把上一版 test 里的样本重新分到 train/val |
| `new_to` | 只在 `continue` 时有：新样本去 `test` 还是 `trainval` |
| `split_count` | 只在 `split-file` 时有：清单的行数 |
| `label_source` | 只在给了 `--label-source` 时有：同时有 JSON 和 txt 的图用哪一种，`json` 或 `txt` |
| `label_classes` | 只在给了 `--label-classes` 时有：txt 标注按哪张类别表读编号 |
| `class_map` | 只在给了 `--map` 时有：`{"源": "目标"}`，目标是空字符串的是去掉的类别 |

## 怎么划分

1. **用划分清单**（`--split-file`）：清单里写了每个样本去哪，平台照做；清单漏了来源里的样本，或者写了来源里没有的，就不建。
2. **接着上一版**（同名快照已有版本，且没加 `--fresh`）：
   - 上一版就有的样本，留在原来的划分（`--test-dirs` 也不会挪动它们）；
   - 新样本默认进 test，`--new-to trainval` 就按比例分进 train/val；
   - `--merge-test`：上一版 test 里的样本（`--test-dirs` 下的除外）重新分到 train/val，新的 test 只剩新样本和 `--test-dirs` 下的样本；
   - 上一版有、这次来源里没有的样本，就不在新快照里了（统计里的「移除」）。
3. **重新划分**（第一版，或加了 `--fresh`）：`--test-dirs` 下的样本进 test，其余的按比例分 train/val。

按比例分 train/val 时：把要分的样本按 `sha256(种子 + "\n" + 标识)` 排序，前 `round(样本数 × val)` 个进 val，其余进 train。同样的样本、比例和种子，分法总是一样；加了新样本，原来的样本之间的先后顺序也不变。

`--test-dirs` 的目录可以写来源版本里的路径前缀（`2026-10`），也可以带上现场和产线（`SES/TX/2026-10`）。写了但一个样本都没匹配上的，当作写错，不建快照。

## 不建快照的情况

预览（`--dry-run`）和建快照时，服务器检查下面这些，有任何一项就不建，全部列出来：

- 来源是训练集快照，或者来源数据集没有填现场或产线；
- 两个来源里有同一个标识（同一现场、产线下的同一路径）；
- 有标注没有对应的图片；有同名不同扩展名的图片；一张图同时有 JSON 和 txt 标注又没给 `--label-source`，或者有两个同一种的标注；一个标注同时配到两张图（见[标注怎么配对](#标注怎么配对)）；
- 标注读不了（不是 UTF-8、不是合法 JSON、没有 `shapes` 列表、形状没有类别名、坐标不是数、`.txt` 编号不是整数或不是 5 个数、坐标不在 0 到 1 之间）；
- 标注里有类别表以外的类别（类别映射之后），或者 `.txt` 编号超出它的类别表（`--label-classes`，没给就是 `--classes`）；`--map` 的源类别在标注里一个框也没有；
- 有不能当检测框的形状（`point`、`line`、`linestrip`、`circle` 等），有完全在图片外的框，有读不出图片尺寸的 JSON 标注；
- 划分清单和来源对不上，`--test-dirs` 有没匹配上的目录；
- 和同名快照的最新版完全一样（样本、划分、标注、类别表都没变）；
- 预览之后有人建了同名快照的新版本（要重新运行，按新的最新版预览）。
