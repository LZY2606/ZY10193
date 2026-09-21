# 碳纪枝径

“碳纪枝径”是一个本地运行的放射性碳测年校准演示服务。它把实验年龄与误差通过**本地、版本化校准曲线**映射到固定日历年网格上的后验密度，保留多个参数分支，并清楚显示每个不连续区间的实际概率质量、先验来源、纪年方向和截断诊断。

## 安装与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 5533
```

访问：

```text
http://127.0.0.1:5533
```

页面标题应显示“碳纪枝径”。

SQLite 默认写入项目根目录的 `carbon_branch.db`；测试通过 `CARBON_DB` 使用临时数据库，不污染交付数据库。

## 数据口径

### 输入字段

CSV 或 JSON 行支持以下字段和常见别名：

- `code`：样本编号；也接受 `id`、`sample`、`编号`、`实验室编号`。
- `age_bp`：实验放射性碳年龄 BP；也接受 `age`、`c14_age`、`实验年龄`。
- `sigma_bp`：一倍标准差；也接受 `sigma`、`sd`、`error`、`标准差`、`误差`。
- `material`：材料类型；支持 charcoal、wood、plant、bone、peat、shell、marine、fish 及中文别名。
- `reservoir_correction`：可选库效应/储库效应校正，默认 0；也接受 `reservoir`、`delta_r`、`库效应校正`。

固定导入文件在 `fixtures/imports/fixed-samples.csv`。库效应校正在似然目标上处理：

```text
校准目标 BP = 实验年龄 BP - reservoir_correction
```

### 本地校准曲线

曲线文件位于：

- `fixtures/curves/LOCAL-13k-v1.csv`
- `fixtures/curves/LOCAL-13k-v2.csv`

列为 `calendar_bp,c14_age,c14_sigma`。应用启动时把完整曲线节点、节点数量和 SHA-256 指纹存入 SQLite。每个分支记录曲线 ID 和指纹；曲线节点完整入库，所以旧分支不依赖之后文件是否修改即可重放。

两个版本的结果**禁止直接合并**：页面和 API 只并列分支，不生成跨曲线版本的加权平均年代。`LOCAL-13k-v1` 的平台/波动段设置使 `5000 ± 35 BP` 的单条测年在固定网格上映射为三个分离年代峰。

### 固定网格与插值

- 日历支撑域：`0–12000 cal BP`。
- 固定边界：共 1201 个，间隔 10 年。
- 积分单元：共 1200 个，中心位于 5、15、…、11995 BP。
- 曲线年龄与曲线误差在单元中心做线性插值。
- 实验误差与曲线误差按平方相加：`sqrt(measurement_sigma² + curve_sigma²)`。
- 后验未归一化值为 `likelihood × prior`，再用固定网格的矩形积分归一化。

显示缩放只改变图形，不重新归一化，也不改变区间概率。

### 先验

内置两个固定版本：

- `uniform-v1`：整个日历支撑域内均匀。
- `stratified-v1`：`0.7` 层内均匀 + `0.3` 全局均匀；陆生材料、库效应材料和未归属材料使用固定层位。

页面显示每个先验版本的来源说明和实际选中层位；分支结果中保存先验配置快照。

## 最高密度区间

最高密度范围按离散网格处理：

1. 按后验密度从高到低排序整格。
2. 逐格累计整格概率质量。
3. 当累计质量首次达到请求概率时停止。
4. 跨过目标概率的最后一个网格**整格保留**，记录实际包含质量，不切开格子伪造亚格精度。
5. 相邻被选格合并为连续区间；未选格形成不连续区间。

默认请求 `0.683` 与 `0.954` 两种覆盖概率，也可以输入 1–2 个位于 `(0,1]` 的概率。每个区间返回起止边界、该区间质量、峰中心、格数、是否触及固定网格边缘；每个 HPD 组返回请求概率、实际离散格质量、密度截止值和跨越格中心。

## 纪年方向

分支必须保存并显示方向：

- `BP_OLDER_POSITIVE`：cal BP，越向过去/越老数值越正，图上向右更老。
- `FORWARD_YOUNGER_POSITIVE`：公元纪年方向，越年轻/公元后为正；内部仍以 cal BP 计算，显示值为 `1950 - cal_BP`，负值为 BCE，正值为 CE。

切换方向不会重新混合概率，只改变显示坐标；方向是分支唯一性参数的一部分。

## 页面操作

1. 点击“导入固定 fixture”，或粘贴 CSV/JSON 行。
2. 选择样本。
3. 选择曲线版本、先验、纪年方向和覆盖概率。
4. 创建分支；重复同一参数会返回并重放原分支。
5. 对同一样本切换到另一个曲线版本会创建并保留另一个分支。
6. 查看密度、先验、CDF、橙色 HPD 区间、区间质量表和截断诊断。
7. 在“运行记录”中刷新或导出 JSON。
8. 可清空业务数据库；曲线会自动重新播种，然后重新导入 fixture 复核。

## API 摘要

- `GET /api/curves`：曲线版本、节点数、支撑域和指纹。
- `GET /api/priors`：固定网格和先验版本来源。
- `POST /api/import/fixture`：导入固定样本。
- `POST /api/import`：导入 CSV 文本或 JSON 记录。
- `GET /api/samples`：样本列表。
- `POST /api/samples/{sample_id}/branches`：创建或重放分支。
- `GET /api/branches?sample_id=...`：查看样本分支；不带参数查看全部并列分支。
- `GET /api/branches/{branch_id}`：查看完整密度数组、CDF、区间和诊断。
- `POST /api/branches/{branch_id}/replay`：按分支快照重放。
- `GET /api/runs/export`：导出运行记录 JSON。
- `POST /api/admin/clear`：删除样本、分支、运行记录和曲线后重新播种固定曲线。

## 清空后重新导入复核

```bash
.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    print(client.post('/api/admin/clear').json())
    print(client.post('/api/import/fixture').json())
    curves = {c['curve_id']: c for c in client.get('/api/curves').json()}
    sample = client.get('/api/samples').json()[0]
    payload = {
        'curve_id': 'LOCAL-13k-v1',
        'prior_key': 'uniform-v1',
        'direction': 'BP_OLDER_POSITIVE',
        'coverages': [0.683, 0.954],
    }
    branch = client.post(f"/api/samples/{sample['sample_id']}/branches", json=payload).json()['branch']
    for region in branch['result']['highest_density_regions']:
        print(region['requested_probability'], region['actual_probability'], region['interval_count'])
PY
```

预期 `0.683` 与 `0.954` 都形成三个不连续区间；`actual_probability` 是实际选中的整格质量之和，`cell_split=false`。

## 自动化测试

```bash
.venv/bin/python -m pytest -q
```

测试覆盖：

- 首页标题和两条版本化曲线播种。
- 固定网格归一化。
- 两种覆盖概率下的三个分离区间。
- 实际离散质量等于所选整格质量之和且不拆格。
- 两种纪年方向的显示与正方向标识。
- 切换曲线版本后分支隔离、旧分支重放。
- 分层先验来源、材料映射和库效应目标。
- 清空数据库、重新播种、重新导入与运行记录导出。
- 中文 CSV 别名导入。
