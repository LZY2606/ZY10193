# 碳纪枝径

放射性碳测年在校准曲线上常常不是一个正态年代：曲线摆动与平台会把一个实验年龄
（例如 800 ± 40 ¹⁴C BP）映射成多个互不相连的日历年区间。**碳纪枝径**用本地、
带版本号的校准曲线在固定日历网格上重建完整后验密度，保留同一测年的多个参数分支，
并把每个区间的概率质量、先验来源、截断误差和重放哈希全部摊开给使用者核对。

## 安装与演示

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 5533
```

浏览器访问 <http://127.0.0.1:5533>，页面标题为 **碳纪枝径**。

首次启动会自动在 `carbon_branches.db` 建表，并从 `fixtures/curves.json` 播种两条
不可变的本地合成校准曲线。设置 `CARBON_DB=/path/to.db` 可更换数据库文件
（测试即使用独立临时库）。

## 数据口径

- **日历坐标**：内部统一为 cal BP，固定网格为 `0–1200 cal BP`、步长 `10 年`，
  共 `121` 个网格边、`120` 个格子。所有曲线版本共用同一网格，曲线在网格边上做
  线性插值，似然/密度定义在格子中心上。
- **实验似然**：对每个格子，使用插值曲线的格子中心 ¹⁴C 年龄 `mu(t)`，权重为
  `exp(-0.5 * ((mu(t) - target)/sigma)^2)`；`target = age - reservoir`，
  `sigma = sd`。日历先验为每个固定格等权，权重之和归一化为 1，与前端缩放无关。
- **库效应（可选）**：导入列 `reservoir`，按 `age − reservoir` 得到有效目标年龄；
  不做任何加权平均年代替代。
- **截断误差 `truncation_mass`**：实验高斯分布落在校准曲线 ¹⁴C 年龄全局范围
  `[min mu, max mu]` 之外的双尾质量 `Phi((min−target)/sigma) + 1 − Phi((max−target)/sigma)`。
  它是固定曲线端点造成的诊断量，单独显示，**不**参与日历密度归一化。
- **最高密度范围（HDR）**：对 120 个格子按密度降序（同密度按下标升序）逐个纳入；
  最后一个格子使累计质量跨过目标概率（68% 或 95%）时停止。返回的 `actual_mass`
  是离散网格的真实包含质量（例如 v1/S-A 的 68% 为 `0.682894309934`、
  95% 为 `0.952476062695`），**绝不切开最后一个格子伪造精度**。相连入选格子合并
  成不连续区间，每个区间单独报告质量与格数。
- **纪年方向**：内部 cal BP 网格从不反转；`age_direction` 只控制页面显示。
  `oldward_positive` 表示纪年正方向朝古老（cal BP 向右增大），
  `youngward_positive` 表示纪年正方向朝年轻（cal BP 向右减小）。方向会写入分支、
  结果和重放哈希，并在页面横幅与坐标轴箭头明确显示。

### 固定夹具

- `fixtures/curves.json`：两条**本地合成教学曲线**（非真实 IntCal 数据），节点单位
  为 cal BP / ¹⁴C BP，均覆盖 0–1200 cal BP：
  - `local-synthetic-v1`：三摆动曲线，470–530 cal BP 有平台；800 ± 40 的单测年
    恰好产生三个分离年代峰，68% HDR 为 3 个区间，95% HDR 也为 3 个区间。
  - `local-synthetic-v2`：修订版，平台移到 460–540 cal BP，摆动幅度不同。
- `fixtures/samples.csv`：固定导入样本。`S-A … S-D` 为单测年（含壳类 reservoir
  校正与靠近端点的截断样例）；`T1/T2/T3` 带 `layer`，用于分层先验。

## 先验与分支

- **均匀日历先验**：每个固定格等权，适合单测年。
- **分层硬顺序先验（可选）**：`layer` 升序表示“年轻 → 古老”，要求
  `theta_1 <= theta_2 <= …`（cal BP 非降）。固定网格上用前向/后向累积求和求各样本
  边缘后验，保留完整分布；同一组共用一个 `group_key`，重放时整组重新联合推断。
  每个 `layer` 必须唯一且非空（不处理同层并列）。
- 同一测年切换曲线版本、方向或先验都会生成**独立分支**并永久保留；不同曲线版本的
  结果没有任何合并接口，导出恢复时还会校验曲线节点哈希与结果哈希，禁止跨版本混入。

## HTTP 接口（摘要）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 操作页面（标题“碳纪枝径”） |
| GET | `/api/curves` | 校准曲线版本摘要（含 `knot_hash`） |
| POST | `/api/import` | 上传 CSV/JSON 样本（列：`code,age,sd,material,reservoir,layer`） |
| POST | `/api/branches` | 单测年 + 均匀先验生成参数分支 |
| POST | `/api/branches/stratified` | 多个带 `layer` 样本生成分层分支组 |
| GET | `/api/branches` / `/api/branches/{id}` | 分支列表 / 完整密度结果 |
| POST | `/api/branches/{id}/replay` | 用当前输入与版本化曲线重算并比对 SHA-256 |
| GET | `/api/export` | 导出全部样本、分支、结果、曲线摘要与运行记录 |
| POST | `/api/import-runs` | 清空状态下恢复运行记录（校验哈希） |
| POST | `/api/reset` | 清空样本/分支/结果/日志，保留版本化曲线 |

## 重放与清空后复核

1. 在页面点「导出运行记录」，或 `curl -o runs.json http://127.0.0.1:5533/api/export`。
2. 点「清空数据库」（或 `curl -X POST .../api/reset`）；曲线夹具仍保留。
3. 点「导入记录」选择 `runs.json`（或 `/api/import-runs`）。工作区非空时导入会被
   拒绝（409）；结果哈希或曲线节点哈希被篡改会被拒绝（400）。
4. 对任意分支点「重放当前分支」，或调用 `/api/branches/{id}/replay`：
   `match=true` 表示重算密度、HDR 与存储的 SHA-256 完全一致。分层分支会按
   `group_key` 自动找回同组样本联合重算。

## 测试

```bash
.venv/bin/python -m pytest -q
```

测试覆盖：固定网格与归一化、v1 三峰与两种覆盖概率的离散实际质量、末格不切分、
方向只影响显示、reservoir 与截断尾部、分层顺序边缘、CSV 导入、两版本分支并存、
运行记录导出/清空/恢复/哈希重放、以及非空库与篡改文件拒绝。
