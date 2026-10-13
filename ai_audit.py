# -*- coding: utf-8 -*-
"""
AI 审核：爬虫结果 -> DeepSeek 逐家审核（产业链环节 + AI关联度 + 数据质量）

用法（在补采目录命令行或给 bat 用）：
  3_AI审核.bat                    默认审核 qcc_补采结果.csv，模型 deepseek-v4-flash
  ai_audit.py --file=xxx.csv      换输入
  ai_audit.py --model=deepseek-v4-pro   用更强的模型（更贵，建议只复核低置信度的）
  ai_audit.py --limit=10          先试 10 家
  ai_audit.py --no-resume         不跳过已审核的

输出：ai_审核结果.csv（固定文件名，重跑自动续跑）
"""
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
# 可选：全局模型清单（某些工具的自定义模型配置）。不设就只用 config.json / 环境变量
MODELS_JSON = Path(os.environ.get("MODELS_JSON_PATH", "") or (HERE / ".models.json"))
CONFIG_JSON = HERE / "config.json"          # 本目录配置（优先）：base_url/model/api_key
DEFAULT_MODEL = "deepseek-v4-flash"
IN_DEFAULT = HERE / "qcc_补采结果.csv"
OUT_DEFAULT = HERE / "ai_审核结果.csv"

OUT_COLS = ["序号", "输入企业名", "输入信用代码", "抓取状态",
            "环节", "环节名称", "环节置信度", "环节理由",
            "AI关联度", "AI关联理由", "疑似官网", "数据异常", "处理建议",
            "审核标记", "审核状态", "AI备注"]

SYSTEM_PROMPT = """你是企业产业链数据审核员，负责给产业链名录里的企业做双标签审核。
【环节体系】（单选主环节，输出字母。下面是一套影视产业链示例，换成你自己的产业体系即可）：
A 技术与设备支撑（AI硬件、拍摄/录制设备、渲染服务器、工具软件开发）
B 数据与算力服务（云计算、算力租赁、数据服务、云渲染、数据中心）
C 内容创意与开发（AI内容生成、剧本/漫剧/短剧/数字内容创作、AIGC应用、动漫游戏开发）
D 拍摄与虚拟制作（影视拍摄、虚拟制片、摄影棚、摄制服务）
E 后期与视效（剪辑、特效、视效、调色、配音）
F 发行与营销（影视发行、广告制作与投放、营销传播）
G 播映与消费（播放平台、影院、票务、直播消费）
H 衍生与综合服务（IP授权、衍生品、产业园区运营、教育培训、综合配套）
【AI关联度】：
L4 核心AI（AI即产品或核心业务）
L3 强AI赋能（AI是核心能力，交付AI+行业方案）
L2 中度应用（局部环节使用AI）
L1 伪AI（只有"智能化/数字化"口号，无实际AI业务）
L0 非AI
【判定规则】：
1. 依据=经营范围关键词+行业分类+企业名称；"技术服务、技术开发、技术咨询、技术推广"属于空话，不作为AI证据。
2. 参考信号示例：人工智能行业应用系统集成服务→L3+；数字文化创意软件开发/动漫游戏开发→C环节；电影摄制服务→D；广告制作→F；广播电视节目制作经营→C或D。
3. 判断不确定时置信度给"低"，处理建议给"人工核实"。
4. 疑似官网：仅当你是知名企业、非常确定其官网时才填 URL；否则留空字符串。不要编造。
5. 数据异常判定（只挑真矛盾，别把正常当异常）：
   - 微型/小型企业参保人数为0 → 正常，不算异常
   - 中型/大型企业参保人数为0 → 异常
   - 参保人数上千但规模标"微型/小型" → 异常（口径矛盾）
   - 经营状态为注销/吊销/清算 → 异常（不应出现在在营名录）
   - 行业分类与企业名称/业务明显风马牛不相及 → 异常
   - 没有以上问题就输出"无"
【输出】只输出一个 JSON 对象，不要 markdown 代码块，不要解释文字：
{"环节":"A-H","环节名称":"环节中文名","环节置信度":"高|中|低","环节理由":"不超过60字","AI关联度":"L0-L4","AI关联理由":"不超过60字","疑似官网":"","数据异常":"无|具体描述(不超过50字)","处理建议":"通过|人工核实|剔除"}"""


def load_cfg(model_id):
    """取 (base_url, api_key, model)。优先环境变量，其次本目录 config.json，最后可选全局 models.json"""
    # 0) 环境变量优先（推荐，密钥不落盘）
    if os.environ.get("DEEPSEEK_API_KEY"):
        return (os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/chat/completions"), os.environ["DEEPSEEK_API_KEY"], os.environ.get("DEEPSEEK_MODEL", model_id)
    # 1) 本目录 config.json
    if CONFIG_JSON.exists():
        try:
            c = json.load(open(CONFIG_JSON, encoding="utf-8"))
            k = (c.get("api_key") or "").strip()
            if k:
                return (c.get("base_url") or "https://api.deepseek.com/chat/completions").strip(), k, (c.get("model") or model_id).strip()
        except Exception:
            pass
    # 2) 全局 models.json
    try:
        d = json.load(open(MODELS_JSON, encoding="utf-8"))
        for m in (d if isinstance(d, list) else d.get("models", [])):
            if m.get("id") == model_id:
                return m.get("url", ""), m.get("apiKey", ""), model_id
    except Exception:
        pass
    return "", "", model_id


def parse_args(argv):
    a = {"file": str(IN_DEFAULT), "out": str(OUT_DEFAULT), "model": DEFAULT_MODEL,
         "limit": 0, "resume": True, "sleep": "0.5,1.2", "temp": "0.3", "think": False}
    for arg in argv:
        if not arg.startswith("--"):
            continue
        k, _, v = arg[2:].partition("=")
        k = k.strip()
        if k == "no-resume":
            a["resume"] = False
        elif k == "think":
            a["think"] = True          # 开启深度思考（更慢更贵，仅建议复核疑难）
        elif k in a:
            a[k] = v
    a["limit"] = int(a["limit"] or 0)
    return a


def chat(url, key, model, system, user, temp, timeout=120, think=False):
    payload = {"model": model, "temperature": temp, "stream": False,
               "max_tokens": 1500,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    if not think:
        # deepseek-v4 是推理模型，默认思考会烧大量 token；关掉又快又省
        payload["thinking"] = {"type": "disabled"}
    r = requests.post(url, headers={"Authorization": f"Bearer {key}",
                                    "Content-Type": "application/json"},
                      json=payload, timeout=timeout)
    r.raise_for_status()
    m = r.json()["choices"][0]["message"]
    return m.get("content") or m.get("reasoning_content") or ""


def parse_json(txt):
    """从回复里抠 JSON（容忍 ```json 包裹、前后废话）"""
    txt = re.sub(r"```(?:json)?", "", txt or "").strip()
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise ValueError("回复里没有 JSON")
    return json.loads(m.group(0))


def build_user_msg(row):
    def g(k):
        return (row.get(k) or "").strip() or "（空）"
    return f"""企业名称：{g('输入企业名')}
统一社会信用代码：{g('统一社会信用代码')}
企业地址：{g('企业地址')}
参保人数：{g('参保人数')}
国标行业：{g('国标行业原文')}
企查查行业：{g('企查查行业原文')}
企业规模：{g('企业规模')}
经营状态：{g('经营状态')}
官网：{g('官网')}
经营范围参考：{g('经营范围')}"""


def done_keys(out_path):
    done = set()
    p = Path(out_path)
    if not p.exists():
        return done
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                done.add(((r.get("输入企业名") or "").strip(),
                          (r.get("输入信用代码") or "").strip()))
    except Exception:
        pass
    return done


def main():
    a = parse_args(sys.argv[1:])
    url, key, model = load_cfg(a["model"])
    a["model"] = model
    if not key:
        print(f"! 找不到 apiKey：请检查 {CONFIG_JSON} 或 {MODELS_JSON}")
        return
    if url.rstrip("/").endswith("/v1"):
        url = url.rstrip("/")[:-3] + "/chat/completions"
    if not url.endswith("/chat/completions"):
        url = url.rstrip("/") + "/chat/completions"

    if not Path(a["file"]).exists():
        print(f"! 找不到输入文件：{a['file']}（先跑完爬虫批量补采）")
        return
    with open(a["file"], "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    done = done_keys(a["out"]) if a["resume"] else set()
    todo = [r for r in rows if ((r.get("输入企业名") or "").strip(),
                                (r.get("输入信用代码") or "").strip()) not in done]
    if a["limit"]:
        todo = todo[:a["limit"]]
    print(f"输入 {len(rows)} 家，已审核 {len(done)}，本次待审 {len(todo)} 家，模型 {a['model']}")

    lo, hi = [float(x) for x in a["sleep"].split(",")]
    mode = "w" if (not Path(a["out"]).exists() or not a["resume"]) else "a"
    ok = fail = 0
    with open(a["out"], mode, newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        if mode == "w":
            w.writeheader()

        for i, row in enumerate(todo, 1):
            name = (row.get("输入企业名") or "").strip()
            state = (row.get("抓取状态") or "").strip()
            base = {"序号": row.get("序号", ""), "输入企业名": name,
                    "输入信用代码": row.get("输入信用代码", ""), "抓取状态": state}
            # 没抓到数据的（未找到/打开失败等）直接过账，不浪费 API
            if state not in ("成功", "部分字段缺失"):
                base.update({"审核状态": "跳过", "AI备注": f"爬虫状态={state}，无数据可审"})
                w.writerow({c: base.get(c, "") for c in OUT_COLS})
                f.flush()
                print(f"[{i}/{len(todo)}] {name} -> 跳过({state})")
                continue

            ans, note = None, ""
            for attempt in (1, 2, 3):
                try:
                    txt = chat(url, key, a["model"], SYSTEM_PROMPT, build_user_msg(row),
                               float(a["temp"]), think=a["think"])
                    ans = parse_json(txt)
                    break
                except Exception as e:
                    note = f"第{attempt}次失败:{str(e)[:60]}"
                    time.sleep(2 * attempt)
            if ans is None:
                base.update({"审核状态": "失败", "AI备注": note})
                w.writerow({c: base.get(c, "") for c in OUT_COLS})
                f.flush()
                fail += 1
                print(f"[{i}/{len(todo)}] {name} -> 审核失败 {note}")
                time.sleep(1)
                continue

            def s(k, maxlen=80):
                v = str(ans.get(k, "") or "").strip()
                return v[:maxlen]
            anomaly = s("数据异常", 60)
            advice = s("处理建议", 10)
            # 审核标记：只有 AI 判定真异常/需人工核实/剔除时才打"待审核"，正常的不打
            flag = "待审核" if (anomaly not in ("无", "") or advice in ("人工核实", "剔除")) else ""
            base.update({
                "环节": s("环节", 4), "环节名称": s("环节名称", 20),
                "环节置信度": s("环节置信度", 4), "环节理由": s("环节理由", 120),
                "AI关联度": s("AI关联度", 4), "AI关联理由": s("AI关联理由", 120),
                "疑似官网": s("疑似官网", 140),
                "数据异常": anomaly,
                "处理建议": advice,
                "审核标记": flag,
                "审核状态": "成功", "AI备注": "",
            })
            w.writerow({c: base.get(c, "") for c in OUT_COLS})
            f.flush()
            ok += 1
            print(f"[{i}/{len(todo)}] {name} -> {base['环节']}/{base['AI关联度']}"
                  f"/{base['处理建议']}" + (f" | ⚠{anomaly}" if flag else ""))
            time.sleep(__import__("random").uniform(lo, hi))

    print(f"\n✅ 完成：成功 {ok}，失败 {fail} -> {a['out']}")
    print("   低置信度/人工核实的，可用 --model=deepseek-v4-pro 再复核一遍")


if __name__ == "__main__":
    main()
