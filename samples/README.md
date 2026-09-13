# 检测样例包（`samples/`）

树莓派手持终端（Pi5 / 800×480 触摸屏 / Python 3.11 + PyQt5）**没有真实毫米波回波、没有 IMU**。
终端播放的每一条响应序列都来自这个目录下预制好的 `response-sequence-v1` 包。
本目录由 `tools/make_samples.py` 生成，是**只读产物**：现场不要手改，要改就改生成器再重新生成。

三套包与剧本一一对应（登记表见 `woodpulse/scenarios.py`，那里是"剧本口径"的单一来源）：

| scenarioId | round | batchId | 计划帧 | 实际回传 | fps | 模型 | state | 文件 | 图片 | 体积 | 剧本节点与用途 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `initial-anomaly-v1` | initial | `scan-Z04-001` | 420 | **386** | 10 | `DEMO-M02` | `interrupted_pause` | 50 | 39 | 6.74 MiB | 第一幕 S10 手持初扫、S12 适用域待核验。第 386 帧后因"平台请求暂停 / 适用域待核验"中断，剩余 34 帧未采集；端侧**不出结论** |
| `reference-samples-v1` | reference | `ref-batch-01` | 240 | 240 | 10 | `DEMO-M02` | `finished` | 36 | 24 | 4.19 MiB | 第三幕 S13 参考样本采集。两条路径（0° 正向 / 90° 换向重复）各 120 帧，四个物理样本 S-01～S-04；**不输出缺陷结论** |
| `rescan-demo-v1` | rescan | `scan-Z04-002` | 420 | 420 | 10 | `DEMO-M02b` | `finished` | 53 | 42 | 7.34 MiB | 第四幕 S19 复扫。完整采完，末段三处异常响应（帧 292 / 330 / 372），端侧初筛 `preliminary` 交平台复核 |

公共字段：`projectId=temple-demo`、`orderId=SH-2026-0901`、`componentId=Z04`、`zoneId=Z04-lower`、
`operatorId=rao`、`deviceId=handheld-02`、`configVersion=CFG-02`、`pointCount=420`、`fps=10`。

三套包的文件数 / 字节 / `datasetHash`（`--verify` 会重算并比对）：

```
initial-anomaly-v1    50 个文件   7071188 B (6.74 MiB)  datasetHash=7b0c1085e1320a0806ca54333bf796fbc169d5458ae978b638f819757da50ccd
reference-samples-v1  36 个文件   4397734 B (4.19 MiB)  datasetHash=4958912ac1891bed01cbd879e19ed13939ac9bbe52c3e26248e3533a2adaee95
rescan-demo-v1        53 个文件   7694253 B (7.34 MiB)  datasetHash=fd85509bf7318fefbda9d06fdabd35069e938b77c5f0c59f9ff2f7abe48af3e7
```

---

## 1. 每套包里有什么

| 文件 | 作用 | 备注 |
|---|---|---|
| `manifest.json` | 数据包清单 + `datasetHash` + `files[]` | **最后写入**；一旦落地就表示"这批数据完整" |
| `batch.json` | 批次元数据 | 含 `plannedFrames` / `returnedFrames` / `state` / `interruptReason` |
| `config.json` | 采集与补偿配置快照 | 三套包共用同一份 `CFG-02` |
| `plan.json` | 采集计划：测区 / 路径 / 预计帧数 | 参考样本包两条路径，其余一条 |
| `frames.csv` | 长表 `frame_index,sample_index,amplitude,t_ms` | 行数 = 实际回传帧数 × 420 |
| `segments.json` | 逐帧响应段（**一帧一条**） | 含 `peaks[]`、`quality`、`pairedImage` |
| `marks.json` | 人工标记 | 初扫 5 条、复扫 5 条、参考样本空数组 `[]` |
| `marks.csv` | `marks.json` 的人可读副本 | 列：`mark_id,frame_index,zone_id,operator_label,position_source,device_monotonic_ns` |
| `quality.json` | 质量与帧完整性 | 初扫 `expectedFrames=420 / frameCount=386 / missingFrames=34` |
| `result.json` | 端侧初筛结果 | 初扫 `withheld`、参考样本 `none`、复扫 `preliminary` |
| `events.log` | 阶段事件，NDJSON 每行一条 | `tMs` 单调不减，`tMs = frameIndex × 100`（采集内相对时间）。初扫 `domain_check_failed` 落在**帧 279**（`tMs=27900`），事件的阶段顺序：`precheck → capture_start → progress → domain_check_failed → pause_requested → capture_paused → batch_sealed` |
| `dataset.json` | **仅参考样本包**：物理样本分组 | 4 组 × 60 帧 = 240 帧，不重不漏 |
| `images/frame_XXXXX.png` | 每 10 帧一张预览图（640×360） | 与终端预览一致；帧 0、10、… |
| `images/index.json` | 图像索引 | `{frameId, file, relPath, sha256, bytes, width, height, kind}` |

`manifest.files[].role` 取值与终端 `storage._guess_role` 同名：
`frames / segments / marks / marks_csv / quality / result / config / dataset / plan / events / image / image_index / batch`。

---

## 2. 数据模型：响应序列怎么造出来的

一帧 = **基线 + 噪声 + 高斯峰**，逐帧确定、逐帧缓变：

```
基线  base(i) = 0.16 + 0.05·sin(0.21i + s) + 0.035·sin(0.63i + 2s) + 0.02·sin(1.7i + 3s)   # i = 采样点序号
噪声  ±0.012（random.Random(包种子 + 帧号×7919)，每帧独立、与遍历顺序无关）
峰    h(帧) · exp(-((x - px)²) / 0.0009)        # x = i/(pointCount-1)，px = sampleIndex/(pointCount-1)
幅值  clamp(base + 噪声 + Σ峰, 0.02, 0.99)，固定 6 位小数
```

`h(帧)` 是升余弦包络：窗口内从 0 平滑长到目标幅值、保持一段、再平滑衰减到 0。
峰值帧上包络**恰好等于**目标幅值（不是近似值），所以段里的幅值与结果里的分数可以按位比对。

三处异常（复扫，平台剧本口径，与 `result.json` 同源同值）：

| 帧 | 段号 | sampleIndex | 归一化位置 | 峰值幅值 = 结果分数 | 标签 | branch |
|---|---|---|---|---|---|---|
| 292 | `echo-Z04-lower-seg-07` | 122 | 0.29 | **0.71** | 疑似严重受潮区域 | radar |
| 330 | `echo-Z04-lower-seg-11` | 197 | 0.47 | **0.84** | 疑似虫蛀空洞（上部响应区） | fusion |
| 372 | `echo-Z04-lower-seg-13` | 260 | 0.62 | **0.87** | 疑似虫蛀空洞（下部响应区） | fusion |

初扫只有一处**中等响应**：帧 200–386、位置 0.62、目标幅值 **0.52**（刻意压在结论阈值之下，
只够触发后续的适用域检查，不够出结论）。参考样本：S-01 无异常、S-02 轻微受潮（0.30 @0.29）、
S-03 已知缺陷/模拟空洞（0.62 @0.47）、S-04 未知待核验（0.22 @0.62）。

**为什么"峰值幅值"与"结果分数"必须是同一组数**：平台剧本把三处异常钉成 0.71 / 0.84 / 0.87。
如果样例里另算一套幅值，演练时终端画出来的峰高与 `result.json` 的分数就对不上，
评审一眼就能看出数据是编的。生成器因此只维护一份数值：`segments.json` 的
`peaks[].amplitude` 与 `result.json` 的 `findings[].score` 同源，`--verify` 会逐个比对。

两点需要知道的口径差别，都是刻意的：

* `frames.csv` 存的是**合成幅值**（基线 + 噪声 + 峰）。锚点帧上基线与峰叠加会超过归一化上限，
  于是被夹到 `0.99`：复扫的 `quality.saturationPct = 0.0317%`，只有帧 330 / 372 附近的
  两三个采样点 `clipped = true`。这既符合"幅值范围 [0.02, 0.99]"，也让质量字段有真实含义。
* `segments.peaks[].amplitude` 记录的是**剧本口径的峰高**（0.71 / 0.84 / 0.87），
  与 `result.json` 的 `score` 逐位相同；每帧合成后的实测最大值另记在
  `segments[].quality.maxAmplitude`（锚点帧为 0.99）。

---

## 3. 命名与编号规则（与终端契约对齐）

生成器优先 `import woodpulse.contracts`，取不到才退回同值字面量；下面这些口径与
`woodpulse/adapters/replay.py`、`woodpulse/storage.py` 的实际实现一致：

| 项目 | 规则 | 对齐依据 |
|---|---|---|
| `frameId` | `frame-00250`（**5 位补零**） | `replay.ReplayFrame.frame_id` 就是 `f"frame-{i:05d}"` |
| 图片文件名 | `frame_00250.png`（5 位补零） | `storage.BatchWriter.save_image` 的 `f"frame_{i:05d}{suffix}"` |
| 段号 `segmentId` | 默认 `echo-Z04-lower-seg-<三位帧号>`（如 `…-seg-292`） | 编号与 frame 一一对应 |
| 段号（仅 3 处锚点） | 帧 292 / 330 / 372 用 `…-seg-07` / `-11` / `-13` | `result.json` 的 `evidenceSegment` 是剧本写死的字符串，包内必须能解析回同一帧 |
| 标记号 `markId` | `mark-<batchId>-01` | `app_state.Mark.display_label` 取末两位显示"标记01" |
| 时间轴 | `tNs = deviceMonotonicNs = frameIndex × 100_000_000`；`frames.csv` 的 `t_ms = frameIndex × 100.0` | 三者是同一把尺子：毫秒 × 10⁶ = 纳秒，样例包不用真机时钟 |
| `frames.csv` 数值格式 | 幅值 `%.6f`、`t_ms` `%.1f` | `storage.BatchWriter.append_frame` 同款 |
| `marks.csv` 空方向 | 写空字段（`marks.json` 里 `operatorLabel = ""`） | 任务契约把 `marks.csv` 定义为 `marks.json` 的人可读副本，保持逐字一致 |
| `createdAt` 等时间 | 固定常量字符串 | 不取当前时间，保证"什么时候生成都长一样" |

`events.log` 的 `tMs` 只记"与帧号对齐的单调时间"（帧 279 → `tMs=27900`）。剧本里"第 279 帧
≈ 演出 28:04"是演示时间轴上的说法，属于现场讲解口径，不作为包内字段 —— 包内时间必须能由帧号
推出来，才能保证重跑得到同样的字节。

补充说明：

* **`frameId` 为什么用 5 位**：任务书里的 `frame-0007` 是格式示意；终端 `replay.py` /
  `storage.py` 真实生成的是 5 位补零。样例包将来要被终端的截图与上传流程引用，
  统一成 5 位才不会出现"样例包的 frameId 和真机批次对不上"。
* **`marks.json` 比契约多两个字段**：`frameIndex` 与 `imageOk`。`contracts.MARK_KEYS` 没有它们，
  但终端 `app_state.Mark.to_dict()` 会写、`storage.BatchWriter.write_marks_csv` 会读
  `mark["frameIndex"]`，少了它 `marks.csv` 的 `frame_index` 列就是空的。
* **`manifest.createdAt` 用了合法时刻**：契约示例写的是 `2026-09-11T31:26:00Z`，小时越界不是
  合法 ISO-8601（任何解析器都会抛异常）。这里按同一分钟写成 `2026-09-11T13:26:00Z`，
  三套包的时间线自洽：配置发布 12:41 → 初扫 12:42 → 参考样本 13:05 → 复扫 13:26。
* **段号为什么只给峰值帧锚点号**：异常窗口内还有几十帧带着同一个峰（幅值更小）。
  如果它们都叫 `seg-07`，段号就不再唯一，"证据段"也就无法定位到具体帧 ——
  这正是 `--verify` 的"segmentId 重复"检查会拦住的情况。

---

## 4. `datasetHash` 算法（必须一致，终端与平台都按这个核对）

> 对该包内**除 `manifest.json` 以外**的所有文件，按**路径字典序**（`a/b` 形式的相对路径）
> 拼接 `path + "\0" + sha256(file) + "\n"`，再对这串字节取 sha256。

伪代码：

```python
h = sha256()
for path in sorted(所有相对路径):        # 排除 manifest.json，排除 *.tmp
    h.update(path.encode("utf-8"));  h.update(b"\0")
    h.update(sha256_file(path).encode("utf-8")); h.update(b"\n")
dataset_hash = h.hexdigest()
```

* 排除 `manifest.json` 是必须的：它自己要装 `datasetHash`，算进去就自指了。
* 与终端 `storage.BatchWriter.commit_manifest` 是**同一个算法**；`--verify` 用同一段代码重算，
  谁都不能各写一套。
* 任何文件内容变了（哪怕只改一个字节），`datasetHash` 就会变 —— 平台据此判断
  "终端播的和平台看的是不是同一份样例"。

---

## 5. 重新生成 / 校验

在项目根目录 `F:\1\pi5`（树莓派上是 `/opt/woodpulse`）执行，Python 3.11，**只用标准库**：

```bash
python tools/make_samples.py --out samples --force   # 生成三套包（--force 覆盖旧的）
python tools/make_samples.py --verify samples        # 只校验已有包，退出码 0/1
python tools/make_samples.py --self-test             # CI：临时目录生成→校验→再生成比对→删除
```

* `--force` **只删除这三套包的目录**，不会动 `samples/README.md` 或目录里的其它文件。
* 不带 `--force` 时若目标已存在样例包，会拒绝覆盖并提示，避免误删现场数据。
* `--verify` 可以指向样例根目录（校验三套），也可以直接指向某一个包目录。

`--verify` 的检查清单：

| # | 检查 |
|---|---|
| 1 | 必需文件与 `images/` 目录齐全（`batch/config/plan/frames/segments/marks/marks.csv/quality/result/events/images/index`） |
| 2 | `manifest` 必填字段齐全且公共字段取值正确（projectId / orderId / zoneId / operatorId / deviceId / configVersion / axis / pointCount / fps / state / createdAt 合法） |
| 3 | `manifest.files[]` 与磁盘文件集合一致，且逐文件 `sha256`、`bytes` 相符 |
| 4 | `manifest.files[].role` 与文件类型一致 |
| 5 | **重算 `datasetHash`** 与 manifest 一致 |
| 6 | `frames.csv`：表头、行数 = `returnedFrames × pointCount`、帧号连续、每帧点数完整、幅值 6 位小数且在 [0.02, 0.99]、`t_ms` 与帧号对齐 |
| 7 | `segments.json`：条数 = 回传帧数、`segmentId` 唯一、`frameId` 在范围内、`frameIndex` 与 `frameId` 一致、`sampleCount`/`axes`/`sourceMode`/时间轴正确、`pairedImage` 存在、`normalizedX` 与 `sampleIndex` 换算一致 |
| 8 | `marks.json`：字段齐全、`frameId` 在范围内、`cameraAssetId` 指向真实图片、`positionSource` 恒为 `operator_tag`、`operatorLabel` ∈ {正面,右侧,背面,左侧,自定义,空} |
| 9 | `marks.csv` 列名与逐行内容同 `marks.json` 一致 |
| 10 | `quality.json`：`expectedFrames == manifest.frameCount`、`frameCount == returnedFrames`、`missingFrames == 计划 − 实际`、`nanValues/duplicateFrames = 0`、`note` 非空 |
| 11 | `result.json`：`conclusion` 合法；每条 finding 的 `evidenceSegment` 能在 `segments.json` 里解析到同一帧，且**该段峰值幅值 == 该条 score** |
| 12 | `images/index.json`：文件存在、PNG 签名与 `IEND` 完整、`sha256` 相符、尺寸 640×360、数量 = 每 10 帧一张 |
| 13 | `events.log`：每行合法 JSON、字段齐全、`tMs` 单调不减且在采集时长内 |
| 14 | `plan.json`：路径字段齐全且帧段在范围内、`expectedFrames` 与 manifest 一致 |
| 15 | `dataset.json`（仅参考样本包）：4 组、帧号恰好覆盖全部 240 帧（不重不漏）、S-04 为"未知待核验" |

`--self-test` 在 `--verify` 之外多做一件事：**把三套包生成两遍并逐文件比对 sha256**，
用来证明"同样输入每次生成字节完全一致"。当前结果：142 个文件全部一致。

---

## 6. 可复现性（为什么重跑一定得到同样的字节）

* 随机数只有一处：`random.Random(包种子 + 帧号 × 7919)`，种子是文件头常量
  （`SEED_INITIAL=20260901` / `SEED_REFERENCE=20260902` / `SEED_RESCAN=20260903`）。
  **没有**全局 `random` 状态、没有 `uuid4()`、没有 `time.time()`。
* 时间字段全是固定常量或由帧号推导（`tMs`/`tNs`/`createdAt`/`finishedAt`）。
* 文本文件统一 `newline="\n"`（LF）+ UTF-8：Windows 上默认会把 `\n` 翻成 `\r\n`，
  那样同一份样例在不同平台生成的 sha256 就会不同。
* PNG 只写 `IHDR/IDAT/IEND`，**不写 tIME/tEXt**；逐行 filter 固定（第 0 行 None、其余行 Up），
  压缩参数固定。
* 背景图与帧号无关，每套包只渲染一次再逐帧复制，既快又稳。

跨环境提醒：PNG 的 IDAT 由 `zlib.compress(..., 9)` 产生，**同一 zlib 版本内字节完全一致**；
更换 zlib 主版本理论上可能产生不同的压缩流（解压后的像素完全相同）。要在异地复现同一份
`datasetHash`，请用生成时记录的 zlib/Python 版本，或直接分发已生成的包 —— 这本来也是样例包的用法。

---

## 7. 画面说明（`images/*.png`，640×360）

* 背景：银灰到暖灰的**柱面渐变**（模拟木柱表面）+ 若干竖向木纹 + 纵向柔和明暗。
* 底部：白色半透明标签条，内容是 5×7 点阵写的机器码 ——
  初扫/复扫为 `Z04-LOWER F0260`，参考样本额外带样本号 `Z04-LOWER F0060 S-03`。
  （画不出真字，所以自带了只覆盖 `0-9 A-Z - . _` 和空格的极小字模表。）
* 进度竖线：位置 = `frameIndex / frameCount`。初扫只到 386/420，线停在约 92% 处，
  一眼能看出"这批没采完"。
* 初扫包：**帧 96 起**在画面下部中间画一个琥珀色矩形（要能看出位置），到中断帧为止。
* 复扫包：**帧 250 起**画三个标记 —— 琥珀矩形（中下，疑似受潮）、
  红色矩形（左上偏中）、红色矩形（右下），后两者代表疑似空洞。
* 参考样本包：四个样本用不同颜色的边框区分（S-01 绿 / S-02 蓝 / S-03 橙 / S-04 紫），
  并在画面上方画一条带 `0.0 / 0.5 / 1.0` 刻度的标尺。

---

## 8. 现场提示

* 这些包是**预制样例（replay）**，不是雷达实采，也不代表木柱内部真实结构 ——
  `manifest.privacyNote` 已写明，界面与平台不要把它当实测数据展示。
* 包内没有 IMU / 标定定位数据，`positionSource` 恒为 `operator_tag`；
  `operatorLabel` 为空字符串表示**操作者没选方向**（界面只显示"标记01"），任何地方都不要猜方位。
* 参考样本的 `dataset.json` 里有一条硬规则：**同一物理样本的连续扫描不得拆到训练集与测试集**。
* 重新生成后 `datasetHash` 会变（内容变了），平台侧要同步更新，否则会被判成"不是同一份样例"。
