# 二手烟暴露干预研究

服务用于组织社区二手烟传感、家庭同意与干预分析，在保护成员身份的同时维持数据质量。

## 运行

```bash
python3 service.py --check                 # 配置自检（不落库）
python3 service.py --init-keys             # 初始化五个角色的本地演示密钥
python3 service.py --port 8000             # 启动服务，GET /health 验活
npm test                                   # 运行全部契约与领域测试
```

身份库与分析库分别落在独立的 SQLite 文件（默认 `identity.db`、`analysis.db`，
可用 `--identity-db` / `--analysis-db` 指定到不同存储位置）。

## 隐私与治理设计

- **身份与分析编号分库**：真实家庭/成员信息、`analysis_id → member_id`
  对应表、同意记录只存在于身份库；分析库中只有匿名编号、匿名场所标签，
  对应关系不可从分析侧重建。
- **角色最小权限**（`X-API-Key`）：现场协调员（家庭/同意/随访）、分析人员
  （场所/设备/读数/冻结估算）、社区反馈员（仅抑制后聚合反馈）、伦理人员
  （血缘追溯、风险登记、定向通知）、管理员（密钥）。
- **人时按真实日期截断**：入组/退出、搬家、同意授予与撤销、控烟政策生效日
  都作为时间线切点；无有效同意的日期不产生任何人时（见 `study/timeline.py`）。
- **离线补传幂等**：按 `(设备序列, 采样窗口起, 采样窗口止)` 唯一去重，
  重传不覆盖原值；无有效校准或校准事后失效的读数**保留但不进入正式估算**，
  排除原因写入冻结血缘。
- **估算冻结不可变**：一次冻结固化方法（含 bootstrap 种子/次数）、权重、
  研究窗口与全部 **204 个地区**的点估计和不确定区间，并有 SHA-256
  `freeze_hash` 可供复算（`GET /estimates/{id}/verify`）；修改只能新建版本。
- **社区反馈小单元格抑制**：去标识家庭数 < 5 的单元格不发布任何暴露数值，
  家庭数只以分桶形式给出。
- **伦理血缘与定向通知**：伦理人员可从聚合结果下钻到读数质量（含排除原因）
  与逐人同意版本/当前状态；圈定分析编号后发起的通知使用**固定话术模板，
  不含任何暴露数值**，由现场协调员在身份库内解析联系方式并落地送达。

## 主要接口

| 方法 & 路径 | 角色 | 说明 |
| --- | --- | --- |
| POST `/households` `/members` `/consents` `/followups` | 现场 | 家庭、成员（返回匿名 analysis_id）、同意、症状随访 |
| POST `/households/{id}/move` `/members/{id}/withdraw` `/consents/{id}/revoke` | 现场 | 搬家/退出/撤销，按日期截断人时 |
| POST `/sites` `/interventions` `/devices` `/calibrations` | 分析 | 匿名场所、政策版本、设备与校准 |
| POST `/readings:bulk` | 分析 | 离线批量补传（去重、校准有效性判定） |
| POST `/timeline/build` | 分析 | 重建人时时间线 |
| POST `/estimates` · GET `/estimates/{id}` `/estimates/{id}/regions` `/estimates/{id}/verify` | 分析 | 冻结、查询、复算 |
| GET `/feedback/{version}` | 社区 | 小单元格抑制后的 204 地区反馈 |
| GET `/ethics/versions/{v}/regions/{r}/trace` · `.../affected` | 伦理 | 血缘追溯与受影响编号圈定 |
| POST `/risks` `/notifications` · GET `/notifications`（伦理，仅编号）· GET `/notifications/{id}`（现场，含联系方式）· POST `/notifications/{id}/deliver` | 伦理/现场 | 风险登记、定向通知与送达闭环 |
| POST `/admin/keys` | 管理员 | 发放与轮换密钥 |

## 代码结构

- `study/database.py`：双库 schema（204 地区预置）
- `study/identity.py`：身份、同意版本、搬家史、随访、风险、通知
- `study/sites.py` / `study/sensors.py`：匿名场所/干预版本；设备校准与补传去重
- `study/timeline.py`：真实日期切点的人时构建
- `study/estimation.py`：加权估算、bootstrap 区间、冻结哈希与血缘
- `study/feedback.py` / `study/ethics.py`：抑制反馈与伦理追溯
- `study/api.py` / `service.py`：HTTP 路由、鉴权与运行入口
