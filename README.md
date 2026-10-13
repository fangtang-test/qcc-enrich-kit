# qcc-enrich-kit — 企查查企业字段补采 + AI 审核

已知一批企业名称 / 统一社会信用代码 → 自动进企查查详情页补齐 10 个字段 → 再用 DeepSeek 逐家 AI 审核。

## 采集字段

统一社会信用代码、企业地址、所属区县、参保人数、国标行业中类/小类、
企查查行业中类/小类、企业规模、官网、经营状态。

实测（非 VIP 账号）：信用代码/地址/区县 100%；参保/行业/规模/经营状态 约 88%；
官网约 47%（多数企业本身未备案）。这 10 个字段基本不依赖 VIP。

## 快速开始

```bash
pip install -r requirements.txt
playwright install chromium          # 或复用已登录的浏览器配置

# 1) 登录态：把已登录的浏览器用户配置放到 ./profile，或设环境变量
export QCC_PROFILE_DIR=/path/to/your/logged-in-browser-profile

# 2) 校准（只查 1 家，验证提取规则）
python qcc_enrich.py --probe

# 3) 批量补采
python qcc_enrich.py --file=company_list.txt --out=custom_result.csv

# 4) AI 审核（DeepSeek）
export DEEPSEEK_API_KEY=sk-xxxxxx     # 或复制 config.example.json 为 config.json 填 key
python ai_audit.py --file=custom_result.csv --out=ai_review.csv
```

Windows 用户可直接双击对应 bat（需保证 `python` 在 PATH 中）。

## 名单格式（company_list.txt，一行一家）

```
示例科技有限公司 | 91110000MA0000000X   # 名称|信用代码（最准）
示例科技有限公司                        # 只有名称
91110000MA0000000X                     # 只有信用代码
```

## 设计要点

- **三层保险**：DOM 选择器 → 行业弹层 → 整页文本正则兜底，字段不会因单点失效而为空
- **防误抓**：按名称搜索时名称对不上直接跳过，不抓错公司
- **不卡死**：搜不到标记跳过；登录态失效立即停车；验证码最多等 120 秒
- **断点续跑**：按输出文件已抓名单跳过，随时中断重开
- **AI 只标真疑点**：中/大型企业参保为 0、参保上千但规模微型、注销/吊销、行业与名称不符才打「待审核」；小微企业参保 0 属正常，不误报

## 输出列

前 14 列为可用数据（序号/输入名/输入码/信用代码/地址/区县/参保/国标中类/国标小类/企查查中类/企查查小类/规模/官网/经营状态），
尾部 5 列为诊断列（匹配企业名/详情页URL/抓取状态/审核标记/备注），不需要可在 Excel 整列删除。

## 免责声明

本工具仅供学习与技术交流。请遵守企查查用户协议与当地法律法规，
控制访问频率，勿用于商业数据转售。仓库不含任何真实企业数据。
