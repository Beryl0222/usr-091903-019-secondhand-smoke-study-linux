# 二手烟暴露干预研究后端

用于在多个社区组织家庭与公共场所二手烟暴露评估的现场研究后端。
系统在保护妇女、儿童等非吸烟者身份的同时，维持可审计的数据质量：

- **家庭同意与成员**：同意按真实日期形成半开区间，撤回/到期自动截断；
  成员搬家、退出均按日期截断人时；
- **身份分库**：家庭身份（姓名、联系方式、住址）与分析编号
  （`A-000001` 样式）分表存放，只有伦理/管理员可解析且逐次审计；
- **匿名场所与传感器**：公共场所只有粗类型与匿名编号；离线设备补传按
  “设备序列 + 采样窗口”去重；校准撤销或窗口外的读数**保留但标记
  invalid**，不进入正式估算；
- **人时与症状**：人天贡献物化时同时施加同意、居住、退出、政策生效
  四道截断；缺测日保留为 `measured=False`，不与真实下降混淆；
- **估算冻结**：覆盖 **204 个地区**，按年龄段直接标准化，同时输出粗
  均值（识别人口构成变化）与 95% 不确定区间；方法、权重、区间、逐地区
  结果与输入行指纹一并冻结，之后不可修改；
- **分层反馈**：社区只收到社区/粗场所类型粒度、经小样本抑制的反馈；
  伦理可从聚合结论逐层追到同意与数据质量；定向通知只发给受影响家庭，
  通知载荷不含任何暴露数值。

## 运行

```bash
python3 service.py --check          # 配置检查 + 演示链路装配 + 审计链校验
python3 service.py --port 8000      # 仅健康检查 /health
python3 service.py --demo --port 8000   # 装配演示数据并开放全部接口
npm test                            # 运行全部契约与领域测试（60 项）
```

领域接口用 `X-Actor-Role` 请求头标识角色：
`field`（现场）、`analyst`（分析）、`community`（社区）、
`ethics`（伦理）、`admin`（管理员）。生产部署中该头应由网关注入并剥离
外部传入值。

## 主要接口

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| GET | `/regions` | 任意 | 204 个地区目录 |
| POST | `/households` `/members` `/consents/grant` `/consents/end` | field | 家庭、成员、同意区间 |
| POST | `/members/withdraw` `/members/move` | field | 退出、搬家（按日期截断） |
| POST | `/devices` `/calibrations` `/deployments` `/readings/batch` | field | 设备、校准、部署、补传去重 |
| POST | `/exposure/materialize` | analyst | 物化人天（四道截断，只增进版本） |
| POST | `/symptoms` | field | 症状随访（窗口外保留并标记） |
| POST | `/estimates/run` `/estimates/freeze` | analyst | 估算与不可变冻结 |
| GET | `/freezes/{id}` `/freezes/{id}/verify` | analyst/ethics | 读取/复核冻结 |
| GET | `/communities/{id}/report` | community | 小样本抑制后的社区反馈 |
| GET | `/ethics/freeze/{id}/overview` 等 | ethics | 聚合 → 地区 → 编号逐层溯源 |
| POST | `/ethics/identity` | ethics | 凭风险理由解析身份（审计） |
| POST | `/notifications` | ethics | 定向通知（不含暴露值，按户去重） |
| GET | `/audit` | ethics/admin | 哈希链审计记录 |

## 代码结构

```
study/
  catalog.py    204 地区、社区、匿名场所、年龄段、干预版本
  identity.py   身份保险库（与分析编号分库）
  sensors.py    设备/校准/部署/读数去重与质量计数
  exposure.py   人时截断、贡献物化（含补传取代版本）、症状随访
  estimates.py  直接标准化、粗均值对比、不确定区间、冻结
  feedback.py   社区反馈抑制、伦理溯源、定向通知
  httpapi.py    路由与角色头
test_*.py       各领域测试与端到端 HTTP 契约
```

设计细节见 [docs/architecture.md](docs/architecture.md)。
