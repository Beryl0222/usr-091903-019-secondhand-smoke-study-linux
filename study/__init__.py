"""二手烟暴露现场研究后端领域包。

模块划分：
- config/database：双库（身份库 identity.db / 分析库 analysis.db）
- access：角色与密钥
- identity：家庭、成员、同意、随访（身份库）
- sites：匿名场所与控烟干预版本
- sensors：设备、校准会话、读数补传去重与有效性
- timeline：按真实日期截断的人时区间
- estimation：覆盖 204 个地区的估算、权重、不确定区间与冻结
- feedback：小单元格抑制的社区反馈
- ethics：伦理血缘追溯与定向通知
"""
