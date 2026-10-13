# -*- coding: utf-8 -*-
"""
企查查「企业字段补采」脚本（独立文件，与 qcc_spider.py 互不干扰）
  · qcc_spider.py  = 按地址反查企业（搜索列表页）
  · qcc_enrich.py  = 已知企业名单 -> 逐家进详情页补字段（本文件）

目标字段（10 项）：
  统一社会信用代码 / 企业地址 / 所属区县 / 参保人数
  国标行业中类 / 国标行业小类 / 企查查行业中类 / 企查查行业小类
  企业规模 / 官网

用法（双击 run_enrich.bat，或在命令行）：
  1) 先校准（强烈建议第一次跑这个）：
     run_enrich.bat --probe
     -> 只查 1 家，把抓到的原始文本导出到 probe_dump.txt，确认字段都抓对了再批量跑

  2) 批量补采（按每日额度分批，支持断点续跑）：
     run_enrich.bat --limit=50          # 本次只查 50 家
     run_enrich.bat --limit=50 --file=你的清单.csv
     run_enrich.bat                     # 不限量，跑到完或被风控拦下

常用参数：
  --file=xxx.csv/.txt 企业清单（默认 company_list.txt；csv 列：序号,企业名称,统一社会信用代码）
  --out=xxx.csv       输出文件（默认 qcc_补采_时间戳.csv，重跑同名文件=续跑）
  --limit=N           本次最多查 N 家（配合 VIP 每日额度）
  --probe             校准模式：只查 1 家 + 导出原始文本到 probe_dump.txt
  --no-resume         不跳过已抓过的（默认自动跳过输出文件里已有的）
  --headless          无头模式（更容易被风控，默认关闭）
  --debug             每家详情页 HTML 存到 debug/ 目录
  --auto              不等待人工确认、遇到验证码直接跳过该家
  --profile=edge_profile   指定登录配置目录（默认：有 edge_profile 用它，否则 qcc_profile）
  --proxy=http://...  指定代理（默认直连）
  --sleep=3,6         每家之间的随机间隔秒数（默认 3~6 秒）
"""
import csv
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

HERE = Path(__file__).resolve().parent

# 浏览器登录态目录：默认用本目录下的 profile/。
# 如果你的登录态在别的地方，用环境变量指过去即可，不写死任何本机路径：
#   Windows:  set QCC_PROFILE_DIR=D:\your\profile
#   macOS/Linux: export QCC_PROFILE_DIR=/path/to/profile
LEGACY_DIR = Path(os.environ.get("QCC_PROFILE_DIR", "") or (HERE / "profile"))
PROFILE_DIR = HERE / "qcc_profile"            # 本目录独立登录态（备用）
EDGE_COPY_DIR = HERE / "edge_profile"          # 本目录复制来的登录态（备用）
LEGACY_PROFILE = LEGACY_DIR / "qcc_profile"    # 老目录：扫码登录态
LEGACY_EDGE = LEGACY_DIR / "edge_profile"      # 老目录：复制的 Edge 登录态

LIST_FILE = HERE / "company_list.txt"         # 默认清单（一行一家 txt；不存在会自动生成模板）
HOME_URL = "https://www.qcc.com/"

# 输出列顺序：先「可用数据」，后「审核/诊断」，
# 审核列全部集中在最尾巴，Excel 里选中后面几列一键删掉即可
DATA_COLS = ["序号", "输入企业名", "输入信用代码", "统一社会信用代码",
             "企业地址", "所属区县", "参保人数",
             "国标行业中类", "国标行业小类", "企查查行业中类", "企查查行业小类",
             "企业规模", "官网", "经营状态"]
AUDIT_COLS = ["匹配企业名", "详情页URL", "抓取状态", "审核标记", "备注"]
OUT_COLS = DATA_COLS + AUDIT_COLS

# ---------------- 从 qcc_spider.py 复用的成熟模块 ----------------


def log(msg):
    print(msg, flush=True)


def wait_enter_or_timeout(prompt, timeout=20):
    """提示后等回车，最多等 timeout 秒就自动继续（防止焦点在浏览器上时卡死）"""
    try:
        import msvcrt
    except Exception:
        input(prompt)
        return
    print(prompt, end="", flush=True)
    start = time.time()
    while time.time() - start < timeout:
        if msvcrt.kbhit():
            ch = msvcrt.getwche()
            if ch in ("\r", "\n"):
                print()
                return
        time.sleep(0.1)
    print(f"（{timeout}秒无输入，自动继续）")


def human_pause(a=3.0, b=6.0):
    time.sleep(random.uniform(a, b))


def body_text(page):
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def waf_blocked(page):
    """企查查 WAF：405 拦截页"""
    return "405.html" in (page.url or "") or "您的访问被阻断" in body_text(page)


def looks_like_captcha(page):
    t = body_text(page)
    return any(k in t for k in ("安全验证", "滑动验证", "请完成验证", "拖动滑块"))


def pick_profile(profile_arg=None):
    """登录态优先级：本目录 edge_profile > 老目录 edge_profile > 老目录 qcc_profile > 本目录 qcc_profile"""
    if profile_arg:
        return profile_arg if os.path.isabs(profile_arg) else str(HERE / profile_arg)
    for d in (EDGE_COPY_DIR, LEGACY_EDGE):
        if (d / "Local State").exists():
            return str(d)
    for d in (LEGACY_PROFILE, PROFILE_DIR):
        if d.exists():
            return str(d)
    return str(PROFILE_DIR)


def open_browser(p, proxy=None, headless=False, profile_arg=None):
    user_dir = pick_profile(profile_arg)
    using_edge_copy = Path(user_dir) in (EDGE_COPY_DIR, LEGACY_EDGE)
    args = ["--disable-blink-features=AutomationControlled", "--no-first-run"]
    args.append("--proxy-server=" + proxy if proxy else "--no-proxy-server")
    if not headless:
        args.append("--start-maximized")
    channels = ("msedge",) if using_edge_copy else ("chrome", "msedge")
    for channel in channels:
        try:
            return p.chromium.launch_persistent_context(
                user_dir, channel=channel, headless=headless,
                viewport=None, args=args, ignore_default_args=["--enable-automation"])
        except Exception:
            continue
    return p.chromium.launch_persistent_context(
        user_dir, headless=headless, viewport=None, args=args,
        ignore_default_args=["--enable-automation"])


# ---------------- 页面抽取 ----------------

EXPAND_JS = r"""
() => {
  const out = [];
  const clickIf = (el, tag) => {
    try { if (el && el.offsetParent !== null) { el.click(); out.push(tag); } } catch (e) {}
  };
  document.querySelectorAll('span,div,a,button,i,em').forEach(e => {
    const t = (e.innerText || '').trim();
    if (!t || t.length > 12) return;
    if (t === '展开' || t === '更多' || t === '查看更多' || t === '详情' || t === '查看详情') {
      clickIf(e, t);
    }
  });
  return out;
}
"""

CLICK_INDUSTRY_JS = r"""
() => {
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const clicked = [];
  // 1) 点「国标行业/企查查行业」标签，触发弹层渲染
  const els = Array.from(document.querySelectorAll('span,div,a,i'));
  for (const e of els) {
    const t = clean(e.innerText);
    if (!t || t.length > 20) continue;
    if (t.includes('国标行业') || t.includes('企查查行业') || t.includes('所属行业')) {
      try { e.click(); clicked.push(t.slice(0, 12)); } catch (err) {}
    }
  }
  // 2) 国标弹层里的「...」折叠节点：点它展开完整层级（含小类）
  document.querySelectorAll('.gb-popover-class [class*="popover"], .gb-popover-class span')
    .forEach(e => {
      const t = clean(e.innerText);
      if (/^[…•\.]{1,6}$/.test(t) && e.children.length <= 2) {
        try { e.click(); clicked.push('expand-dots'); } catch (err) {}
      }
    });
  return clicked.join(',');
}
"""

EXTRACT_JS = r"""
() => {
  const clean = s => (s == null ? '' : String(s))
      .replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').trim();
  const body = clean(document.body ? document.body.innerText : '');
  const pick = re => { const m = body.match(re); return m ? clean(m[1]) : ''; };

  // 企查查字段行通用结构：div.rline > span.f > [span.need-copy-field 标签] + [span.val / .copy-value 值]
  const lines = Array.from(document.querySelectorAll(
      'div.rline, div[class*="rline"], div[class*="basic-item"], div[class*="info-item"], tr'));
  const lineVal = kw => {
    for (const rl of lines) {
      const t = clean(rl.innerText || '');
      if (!t || t.length > 220) continue;
      if (t.indexOf(kw) === -1) continue;
      const cv = rl.querySelector('.copy-value');
      if (cv) return clean(cv.innerText);
      const val = rl.querySelector('.val');
      if (val) return clean(val.innerText);
      return clean(t.replace(kw, '').replace(/^[：:\s]+/, ''));
    }
    return '';
  };

  // 通用：把容器里的层级按"文本节点"逐个取出（分隔符/图标自动忽略）
  // 用户提供的 XPath 证实层级结构是 div[N]/span/span 一组一级，
  // innerText 会把各级粘成一坨，必须按文本节点切
  const SKIP_WORDS = ['国标行业', '企查查行业', '所属行业', '关联产业链', 'Industry'];
  const levelsOf = el => {
    if (!el) return [];
    const out = [];
    const tw = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null, false);
    let n;
    while ((n = tw.nextNode())) {
      for (const part of clean(n.nodeValue).split(/[>＞/／｜|]/)) {
        const t = clean(part);
        if (t && t.length <= 30 && !/^[…•\.]+$/.test(t)
            && !/^[（(][A-Z]?\d{4,8}[）)]$/.test(t)          // 纯代码片段（M7519）不是层级
            && !SKIP_WORDS.includes(t)) out.push(t);
      }
    }
    return out.filter((t, i) => i === 0 || t !== out[i - 1]);   // 去相邻重复（嵌套span）
  };

  // 行业弹层：.industry-cell > .ntag(体系名) + 层级区
  const indCell = tag => {
    for (const cell of document.querySelectorAll('.industry-cell, [class*="industry-cell"]')) {
      const nt = cell.querySelector('.ntag, [class*="ntag"]');
      if (!nt || clean(nt.innerText).indexOf(tag) === -1) continue;
      const it = cell.querySelector('.industry-text, [class*="industry-text"]') || cell;
      const lv = levelsOf(it);
      if (lv.length >= 2) return lv.join('>');
    }
    return '';
  };

  // 国标行业弹层（常驻 DOM，display:none 也能读）：div.gb-popover-class 整个就是国标层级
  const gbPop = (() => {
    for (const pop of document.querySelectorAll('.gb-popover-class, [class*="gb-popover"]')) {
      const lv = levelsOf(pop);
      if (lv.length >= 2) return lv.join('>');
    }
    return '';
  })();

  const titleEl = document.querySelector('h1');

  // 行业原文择优：带行业代码（如 (M7519)）的优先，其次更长的
  const best = arr => {
    const c = arr.filter(Boolean);
    if (!c.length) return '';
    const withCode = c.find(x => /[（(][A-Z]?\d{4,8}[）)]/.test(x));
    if (withCode) return withCode;
    return c.reduce((a, b) => (b.length > a.length ? b : a));
  };

  // 企业规模：rline.scale 行（span.val 直接是"小型"）
  let scale = '';
  const scaleLine = document.querySelector('div.rline.scale, [class*="rline"][class*="scale"]');
  if (scaleLine) {
    const v = scaleLine.querySelector('.val');
    if (v) scale = clean(v.innerText);
  }
  if (!scale) scale = lineVal('企业规模');

  return {
    title:  clean(titleEl ? titleEl.innerText : ''),
    code:   lineVal('统一社会信用代码') || pick(/统一社会信用代码[：:]?\s*([0-9A-HJ-NPQRTUWXY]{18})/),
    addr:   (() => {
              const a = document.querySelector('.textaddress .copy-value, [class*="textaddress"] .copy-value');
              return a ? clean(a.innerText) : (lineVal('注册地址') || lineVal('地址'));
            })(),
    insured: lineVal('参保人数') || pick(/参保人数[^\d]{0,12}([\d,]+)/),
    staff:  lineVal('员工人数'),
    gb:     gbPop || indCell('国标行业'),
    // 工商信息区的「国标行业 xxx（M7499）」：唯一带代码的来源，层级可能被弹层折叠时靠它兜底
    gbline: (() => {
              const m = body.match(/国标行业\s*\n+\s*([^\n（()]{2,30})\s*[（(]([A-Z]?\d{4,8})[）)]/);
              return m ? clean(m[1]) + '（' + m[2] + '）' : '';
            })(),
    qcc:    indCell('企查查行业') || lineVal('企查查行业'),
    scale:  scale,
    site:   lineVal('官网') || lineVal('网址'),
    status: (() => {
              const v = lineVal('经营状态');
              if (/^(存续|在业|开业|注销|吊销|迁出|迁入|清算|停业)/.test(v)) return v;
              const m = body.match(/登记状态[^\n]{0,30}?(存续|在业|开业|注销|吊销|迁出|迁入|清算|停业)/);
              return m ? m[1] : (v || '');
            })(),
    body_head: body.slice(0, 20000)
  };
}
"""

# ---------------- 整页文本兜底（不依赖任何 class 名） ----------------
# 借鉴旧脚本 qcc_spider.py 的思路：先拿整页 innerText，再用「标签→值」宽松正则扫。
# 每个字段给多条正则按优先级排列，任何一条命中就算数，避免单点失效导致空值。
FALLBACK_PATTERNS = {
    "code": [
        r"统一社会信用代码\s*[：:]?\s*\n?\s*([0-9A-HJ-NPQRTUWXY]{18})",
    ],
    "addr": [
        r"注册地址\s*\n?\s*([^\n]{6,140})",
        r"\n地址\s*[：:]?\s*\n?\s*([^\n]{6,140})",
        r"企业地址\s*[：:]?\s*\n?\s*([^\n]{6,140})",
    ],
    "insured": [
        r"参保人数\s*[:：]?\s*\(?\s*([0-9,]+)",
        r"参保人数\s*\n?\s*([0-9,]+)",
    ],
    "gb": [
        # 工商信息区：国标行业 其他未列明专业技术服务业（M7499）
        r"国标行业\s*\n?\s*([^\n（(]{2,30})\s*[（(]([A-Z]?\d{4,8})[）)]",
        r"国标行业\s*[：:]?\s*\n?\s*([^\n]{2,60})",
        r"所属行业\s*[：:]?\s*\n?\s*([^\n]{2,60})",
    ],
    "qcc": [
        r"企查查行业\s*[：:]?\s*\n?\s*([^\n]{2,80})",
    ],
    "scale": [
        r"企业规模\s*[：:]?\s*\n?\s*([^\n]{1,24})",
    ],
    "site": [
        r"官网\s*[：:]?\s*\n?\s*([^\n]{2,140})",
        r"网址\s*[：:]?\s*\n?\s*([^\n]{2,140})",
    ],
    "status": [
        r"登记状态\s*\n?\s*(存续|在业|开业|注销|吊销|迁出|迁入|清算|停业)",
        r"经营状态\s*[：:]?\s*\n?\s*(存续|在业|开业|注销|吊销|迁出|迁入|清算|停业)",
    ],
}

# 登录/VIP 墙：这些字样出现说明该字段受权限限制，不是"数据不存在"
VIP_MARKERS = ("成为VIP查看", "VIP查看", "VIP专享", "登录后查看",
               "请登录后", "登录后可查看", "开通VIP", "立即登录")


def vip_hit(body):
    if not body:
        return ""
    return next((k for k in VIP_MARKERS if k in body), "")


# 所有已知的"下一个字段标签"，用来截断值（防止把英文名、成立日期一起吞进来）
_LABEL_STOP = (r"(统一社会信用代码|企业名称|法定代表人|注册资本|成立日期|营业期限|核准日期|"
               r"登记机关|所属地区|参保人数|国标行业|所属行业|英文名|经营范围|企业规模|"
               r"企查查行业|员工人数|企业族群|官网|网址|电话|邮箱|地址|附近企业|复制|更多)")


def _fb(field, body):
    """在整页文本里按优先级找字段值；找不到返回 ''"""
    if not body:
        return ""
    for pat in FALLBACK_PATTERNS.get(field, []):
        m = re.search(pat, body)
        if m:
            if field == "gb" and m.lastindex and m.lastindex >= 2:
                return f"{m.group(1).strip()}（{m.group(2)}）"
            v = m.group(1).strip()
            v = re.split(_LABEL_STOP, v)[0].strip()
            v = v.replace("复制", "").strip()
            if v:
                return v
    return ""


SCALE_MAP = {"微型": "XS(微型)", "小型": "S(小型)", "中型": "M(中型)", "大型": "L(大型)"}


def normalize_scale(v):
    """统一成名录库 V1 的口径 XS/S/M/L"""
    v = (v or "").strip()
    if not v:
        return ""
    for k, val in SCALE_MAP.items():
        if k in v:
            return val
    return v


def clean_addr(v):
    """去掉工商信息区地址行尾的「（邮编…）」「附近企业」等噪音"""
    if not v:
        return ""
    v = re.sub(r"[（(]\s*邮编[^）)]*[）)]", "", v)
    v = re.sub(r"(附近企业|更多|复制)+$", "", v).strip()
    v = re.split(_LABEL_STOP, v)[0].strip()
    return v


def parse_industry(raw):
    """把行业原文拆成 (中类, 小类, 代码)。兼容 'A>B>C>D (M7519)' 与单行写法"""
    if not raw:
        return "", "", ""
    raw = raw.strip()
    m = re.search(r"[（(]([^）)]{2,20})[）)]", raw)
    code = ""
    if m:
        inner = m.group(1)
        cm = re.search(r"([A-Z]?\d{4,8})", inner)
        code = cm.group(1) if cm else inner
    txt = re.sub(r"[（(][^）)]*[）)]", "", raw)
    parts = [p.strip() for p in re.split(r"[>/｜|、,，]+", txt) if p.strip()]
    if not parts:
        return "", "", ""
    if len(parts) >= 2:
        return parts[-2], parts[-1], code
    return "", parts[0], code


def merge_gb(levels_raw, line_raw):
    """合并国标行业两个来源：
       levels_raw = 弹层层级（门类>大类>中类>小类），末尾可能是被折叠的中类
       line_raw   = 工商信息区「其他未列明专业技术服务业（M7499）」——唯一带代码的来源
    判定：若弹层末级名称 != 工商信息小类名，说明弹层末级是中类（小类被折叠），
          则 中类=弹层末级、小类=工商信息小类。"""
    code = ""
    small_name = ""
    if line_raw:
        _, s, c = parse_industry(line_raw)
        small_name, code = s, c
    levels = [re.sub(r"[（(][^）)]*[）)]", "", x).strip()
              for x in (levels_raw or "").split(">")]
    levels = [x for x in levels if x]
    if not levels:
        return "", small_name, code
    if small_name and levels[-1] != small_name:
        return levels[-1], small_name, code          # 小类被折叠：末级实为中类
    mid = levels[-2] if len(levels) >= 2 else ""
    return mid, levels[-1], code


GZ_DISTRICTS = ["天河区", "海珠区", "黄埔区", "番禺区", "白云区", "南沙区",
                "越秀区", "荔湾区", "花都区", "增城区", "从化区"]


def parse_district(addr):
    if not addr:
        return ""
    for d in GZ_DISTRICTS:
        if d in addr:
            return d
    m = re.search(r"([\u4e00-\u9fa5]{2,8}(?:区|县))", addr)
    return m.group(1) if m else ""


def parse_insured(v):
    if not v:
        return None
    m = re.search(r"([\d,]+)", v)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except Exception:
        return None


def clean_site(v):
    if not v:
        return ""
    v = v.strip()
    if v in ("-", "—", "无", "暂无", "未公布"):
        return ""
    return v


# ---------------- 清单与续跑 ----------------


TXT_TEMPLATE = (
    "# 一行一家企业；# 开头的行是注释，会被忽略\n"
    "# 写法①：只有企业名称（按名称搜，同名企业多时不如按码准）\n"
    "# 写法②：企业名称 | 统一社会信用代码（最准，推荐）\n"
    "# 写法③：只有18位信用代码（直接按码搜，也准）\n"
    "# 例：\n"
    "# 示例科技有限公司 | 91110000MA0000000X\n"
    "# 91110000MA0000000X\n"
)


def load_list(path):
    p = Path(path)
    if not p.exists():
        # txt 清单不存在时生成模板（与老脚本 addresses.txt 同款体验）
        if str(p).lower().endswith(".txt"):
            p.write_text(TXT_TEMPLATE, encoding="utf-8")
            log(f"未找到清单，已生成模板：{p}，往里填企业后重跑。")
        else:
            log(f"! 找不到清单文件：{p}")
        return []
    # ---- txt：一行一家，# 注释，「名称 | 信用代码」或纯名称/纯代码 ----
    if p.suffix.lower() == ".txt":
        out = []
        for i, ln in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            name, sep, code = ln.replace("｜", "|").partition("|")
            name, code = name.strip(), code.strip()
            # 纯代码行：18位码单独一行 = 按码搜（最准）
            if not code and re.fullmatch(r"[0-9A-HJ-NPQRTUWXY]{18}", name):
                code = name
            if not name:
                continue
            out.append((str(len(out) + 1), name, code))
        return out
    # ---- csv：列名 序号/企业名称/统一社会信用代码 ----
    with open(p, "r", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        rows = list(rd)
    out = []
    for r in rows:
        name = (r.get("企业名称") or "").strip()
        code = (r.get("统一社会信用代码") or "").strip()
        seq = (r.get("序号") or "").strip()
        if not name:
            continue
        out.append((seq, name, code))
    return out


def done_keys(out_path):
    """续跑：返回已抓过的 (企业名, 信用代码) 集合"""
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


def build_search_url(key):
    return "https://www.qcc.com/web/search?key=" + quote(key.strip())


# ---------------- 单家抓取 ----------------


def fetch_one(page, seq, name, code, debug=False):
    """返回 dict（含 _state / _note）"""
    qkey = code if code else name
    url = build_search_url(qkey)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        return {"_state": "打开失败", "_note": str(e).splitlines()[0][:60]}
    time.sleep(random.uniform(0.8, 1.5))

    if waf_blocked(page):
        return {"_state": "WAF拦截", "_note": "405"}
    if looks_like_captcha(page):
        return {"_state": "验证码", "_note": "需人工完成"}

    # 搜索页 -> 取第一条 /firm/ 链接（优先取名称完全匹配的，避免同名误抓）
    if "/firm/" not in (page.url or ""):
        # 明确的无结果提示，直接判"未找到"，不等选择器超时
        st_txt = body_text(page)
        if any(k in st_txt for k in ("没有找到", "未找到相关", "暂无数据", "未查询到", "很抱歉，没有")):
            return {"_state": "未找到", "_note": "企查查搜不到该企业（可能非法人/已注销/名称不符）"}
        href = ""
        try:
            page.wait_for_selector('a[href*="/firm/"]', timeout=15000)
            href = page.evaluate(r"""
            (nm) => {
              const clean = s => (s||'').replace(/[\s（）()·、,，\-—]+/g,'');
              const t = clean(nm);
              const as = Array.from(document.querySelectorAll('a[href*="/firm/"]'))
                .filter(x => /\/firm\/[0-9a-zA-Z]+\.html/.test(x.getAttribute('href')||''));
              if (!as.length) return '';
              // 只接受名称能对上的结果；对不上宁可跳过，也不误抓（防串数据）
              const hit = as.find(x => {
                const s = clean(x.innerText);
                return s.indexOf(t) >= 0 || t.indexOf(s) >= 0;
              });
              return hit ? hit.getAttribute('href') : '__NO_NAME_MATCH__';
            }
            """, name)
        except PWTimeout:
            href = ""
        if href == "__NO_NAME_MATCH__":
            return {"_state": "未找到", "_note": "搜索结果与名称对不上（防误抓已跳过）"}
        if not href:
            return {"_state": "未找到", "_note": "搜索无结果（可能非法人主体）"}
        firm_url = href if href.startswith("http") else ("https://www.qcc.com" + href)
        try:
            page.goto(firm_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            return {"_state": "打开失败", "_note": str(e).splitlines()[0][:60]}
        time.sleep(random.uniform(0.8, 1.5))

    if waf_blocked(page):
        return {"_state": "WAF拦截", "_note": "详情页405"}
    if looks_like_captcha(page):
        return {"_state": "验证码", "_note": "详情页需人工验证"}
    # 登录墙：登录态失效时详情页会跳登录页，继续跑只会全空，停车
    _t = body_text(page)
    if ("扫码登录" in _t or "密码登录" in _t) and "统一社会信用代码" not in _t:
        return {"_state": "登录墙", "_note": "登录态失效，详情页跳到了登录页"}

    # 展开 + 点行业标签（让国标/企查查行业显示出来）
    try:
        page.evaluate(EXPAND_JS)
        time.sleep(0.4)
        page.evaluate(CLICK_INDUSTRY_JS)
        time.sleep(0.6)
        # 随机动作：随机挪一下鼠标 + 上下滚动一下再回到中部
        page.mouse.move(random.randint(200, 900), random.randint(150, 600))
        page.evaluate(f"window.scrollTo(0, {random.randint(100, 700)})")
        time.sleep(random.uniform(0.2, 0.5))
        page.evaluate("window.scrollTo(0, 400)")
        time.sleep(0.3)
    except Exception:
        pass

    if debug:
        d = HERE / "debug"
        d.mkdir(exist_ok=True)
        (d / f"{seq or 'x'}_{name[:20]}.html").write_text(page.content(), encoding="utf-8")

    d = page.evaluate(EXTRACT_JS)
    body = d.get("body_head", "")

    # ==== 兜底层：DOM 选择器抓空的字段，一律退回「整页文本 + 宽松正则」再找一遍 ====
    if not (d.get("code") or "").strip():
        d["code"] = _fb("code", body)
    if not (d.get("addr") or "").strip():
        d["addr"] = _fb("addr", body)
    if not (d.get("insured") or "").strip():
        d["insured"] = _fb("insured", body)
    if not (d.get("gb") or "").strip():
        d["gb"] = _fb("gb", body)
    if not (d.get("gbline") or "").strip():
        d["gbline"] = _fb("gb", body)
    if not (d.get("qcc") or "").strip():
        d["qcc"] = _fb("qcc", body)
    if not (d.get("scale") or "").strip():
        d["scale"] = _fb("scale", body)
    if not (d.get("site") or "").strip():
        d["site"] = _fb("site", body)
    st = d.get("status", "")
    if not re.match(r"^(存续|在业|开业|注销|吊销|迁出|迁入|清算|停业)", st or ""):
        d["status"] = _fb("status", body) or st

    gb_mid, gb_small, gb_code = merge_gb(d.get("gb", ""), d.get("gbline", ""))
    qcc_mid, qcc_small, qcc_code = parse_industry(d.get("qcc", ""))
    addr = clean_addr(d.get("addr", ""))
    insured = parse_insured(d.get("insured", ""))
    # 兜底：头部「员工人数」的数据来源就是工商年报参保人数（弹层标注"工商年报 参保"），
    # 工商信息区参保人数缺失时用它补
    insured_src = "工商信息"
    if not insured:
        insured = parse_insured(d.get("staff", ""))
        insured_src = "头部员工人数" if insured else ""

    # VIP 墙甄别：非 VIP 页面上参保人数有时被遮挡而显示 0。
    # 处理原则（用户确认）：0 是页面真实显示值，照记不改；但打"待审核"标记供人工核实
    vw_field = vip_hit(d.get("insured", "")) or vip_hit(d.get("staff", ""))
    masked = bool(vw_field)

    # 匹配校验：信用代码/企业名对不上要留痕（防同名误抓、防串页）
    warn = []
    got_code = (d.get("code") or "").strip()
    if code and got_code and got_code != code:
        warn.append(f"信用代码不符(页面{got_code})")
    if not code:
        norm = lambda s: re.sub(r"[\s（）()·、,，\-—]+", "", s or "")
        if norm(d.get("title", "")) != norm(name):
            warn.append("企业名与搜索词不完全一致")
    if insured_src == "头部员工人数":
        warn.append("参保人数取自头部员工人数(工商年报参保)")

    miss = []
    if not d.get("code"):
        miss.append("信用代码")
    if not insured and insured != 0:
        miss.append("参保人数")
    if not gb_small:
        miss.append("国标行业")
    if not qcc_small:
        miss.append("企查查行业")
    if not d.get("scale"):
        miss.append("企业规模")

    # ---- 待审核标记：凡是有疑点的行统一打标，交人工核实（不篡改数据、不丢弃数据）----
    review = []
    if masked:
        review.append("参保显示0疑似VIP墙遮挡，需人工核实")
    elif insured == 0:
        review.append("参保人数为0，需人工核实（可能是真0/未填报/被遮挡）")
    if gb_small and len(gb_small) >= 12 and ">" not in (d.get("gb") or ""):
        review.append("国标行业层级疑似粘连未拆开，需人工核实")
    if warn:
        review.extend(warn)
    if miss:
        review.append("缺：" + "、".join(miss))
    review_flag = "待审核" if review else ""

    return {
        "序号": seq, "输入企业名": name, "输入信用代码": code,
        "匹配企业名": d.get("title", ""),
        "统一社会信用代码": d.get("code", ""),
        "企业地址": addr,
        "所属区县": parse_district(addr),
        "参保人数": insured if insured is not None else "",
        "国标行业中类": gb_mid, "国标行业小类": gb_small,
        "国标行业原文": d.get("gb", "") + (f" ({gb_code})" if gb_code else ""),
        "企查查行业中类": qcc_mid, "企查查行业小类": qcc_small,
        "企查查行业原文": d.get("qcc", "") + (f" ({qcc_code})" if qcc_code else ""),
        "企业规模": normalize_scale(d.get("scale", "")),
        "官网": clean_site(d.get("site", "")),
        "经营状态": d.get("status", ""),
        "详情页URL": page.url,
        "抓取状态": "成功" if not miss else "部分字段缺失",
        "审核标记": review_flag,
        "备注": "；".join(review) or "",
        "_body_head": d.get("body_head", ""),
        "_state": "ok",
    }


# ---------------- 主流程 ----------------


def parse_args(argv):
    a = {"file": str(LIST_FILE), "out": None, "limit": 0, "probe": False,
         "resume": True, "headless": False, "debug": False, "auto": False,
         "profile": None, "proxy": None, "sleep": "0.3,0.8"}
    for arg in argv:
        if not arg.startswith("--"):
            continue
        k, _, v = arg[2:].partition("=")
        k = k.strip()
        if k in ("probe", "headless", "debug", "auto"):
            a[k] = True
        elif k == "no-resume":
            a["resume"] = False
        elif k == "limit":
            a["limit"] = int(v or 0)
        elif k in ("file", "out", "profile", "proxy", "sleep"):
            a[k] = v
    return a


def main():
    a = parse_args(sys.argv[1:])
    items = load_list(a["file"])
    if not items:
        return

    if a["out"]:
        out = Path(a["out"])
    else:
        # 固定文件名才能断点续跑（带时间戳的话每次重开都是新文件，续跑失效）
        out = HERE / ("probe_校准结果.csv" if a["probe"] else "qcc_补采结果.csv")

    done = done_keys(out) if a["resume"] else set()
    # 列结构升级过：旧结果表表头对不上时，改写到新文件（续跑进度仍认旧文件的已抓名单）
    if a["resume"] and out.exists():
        with open(out, "r", encoding="utf-8-sig") as _f:
            _h = next(csv.reader(_f), [])
        if _h and _h != OUT_COLS:
            out = out.with_name(out.stem + f"_新列版_{datetime.now().strftime('%Y%m%d_%H%M%S')}" + out.suffix)
            log(f"! 结果表列结构已升级，本次结果写入新文件：{out.name}")
    if done:
        items = [x for x in items if (x[1], x[2]) not in done]
        log(f"续跑：已抓过 {len(done)} 家，跳过")

    if a["probe"]:
        # 校准必须选一家正规企业：信用代码 18 位且以 9 开头（91/92/93=企业，
        # 12 开头是事业单位、11 是机关，页面结构不同，不适合做模板）
        idx = next((i for i, x in enumerate(items) if len(x[2]) == 18 and x[2].startswith("9")),
                   next((i for i, x in enumerate(items) if len(x[2]) == 18), 0))
        items = items[idx:idx + 1]
        a["limit"] = 1
        log(f"=== PROBE 校准模式：只查 1 家 -> {items[0][1]}（{items[0][2]}）===")
        log("    原始文本导出到 probe_dump.txt")
    elif a["limit"]:
        items = items[:a["limit"]]

    try:
        lo, hi = [float(x) for x in a["sleep"].split(",")]
    except Exception:
        lo, hi = 0.3, 0.8
    if lo > hi:
        lo, hi = hi, lo

    # 拟人节奏：跑若干家后来一次较长的停顿（模拟真人离开座位），间隔随机 25~40 家
    next_break = [random.randint(100, 150)]

    def pace():
        time.sleep(random.uniform(lo, hi))
        next_break[0] -= 1
        if next_break[0] <= 0:
            brk = random.uniform(8, 15)
            log(f"    ☕ 短暂停顿 {brk:.0f} 秒...")
            time.sleep(brk)
            next_break[0] = random.randint(100, 150)

    log(f"本次待查 {len(items)} 家 -> {out}")

    with sync_playwright() as p:
        ctx = open_browser(p, a["proxy"], a["headless"], a["profile"])
        try:
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome=window.chrome||{runtime:{}};"
                "Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh']});")
        except Exception:
            pass
        page = ctx.pages[-1] if ctx.pages else ctx.new_page()

        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(random.uniform(1.5, 3))
        except Exception as e:
            log(f"打开首页失败：{str(e).splitlines()[0]}")

        if waf_blocked(page):
            log("! 首页就被 WAF 拦截。建议：换个时间再试，或删除 profile/ 目录后重跑 1_先双击我_校准.bat 重新扫码登录")
            if not a["auto"]:
                wait_enter_or_timeout(">>> 按回车继续尝试（20秒后自动继续）... ")

        if not a["auto"] and not a["probe"]:
            log("\n=========== 开工前确认 ===========")
            log("1. 浏览器里确认已登录企查查（未登录先扫码）")
            log("2. 确认能正常打开企业详情页后，回控制台按回车开始")
            log("==================================\n")
            wait_enter_or_timeout(">>> 按回车开始（20秒后自动开始）... ")

        mode = "w" if (not out.exists() or not a["resume"]) else "a"
        with open(out, mode, newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=OUT_COLS)
            if mode == "w":
                w.writeheader()
            f.flush()

            ok = skipped = 0
            dump = []
            nf = 0
            try:
                for i, (seq, name, code) in enumerate(items, 1):
                    log(f"[{i}/{len(items)}] {name}" + (f"（{code}）" if code else "（按名称搜）"))
                    r = fetch_one(page, seq, name, code, a["debug"])
                    st = r.get("_state", "")
                    if st != "ok":
                        row = {c: "" for c in OUT_COLS}
                        row.update({"序号": seq, "输入企业名": name, "输入信用代码": code,
                                    "抓取状态": st, "备注": r.get("_note", "")})
                        w.writerow(row)
                        f.flush()
                        skipped += 1
                        log(f"    ! {st}：{r.get('_note','')}（已记入结果表，继续下一家）")
                        if st == "未找到":
                            nf += 1
                            if nf % 10 == 0:
                                log(f"    … 已累计 {nf} 家搜不到（园区/基地/注销类居多），全部留痕跳过")
                            time.sleep(random.uniform(0.2, 0.6))   # 搜不到的快速跳过
                            continue
                        if st == "验证码" and not a["auto"]:
                            log("    >>> 检测到验证码：请在浏览器里完成（最多等 120 秒，超时自动跳过该家，下次续跑会重试）")
                            solved = False
                            for _ in range(12):
                                time.sleep(10)
                                if not looks_like_captcha(page):
                                    solved = True
                                    break
                            log("    ✓ 等待结束，继续下一家（该家未入已抓名单，下次续跑自动重试）")
                        if st == "WAF拦截":
                            log("    ! 触发风控，停止本次任务（改天再跑，已抓数据已保存）")
                            break
                        pace()
                        continue

                    body_head = r.pop("_body_head", "")
                    r.pop("_state", "")
                    w.writerow({c: r.get(c, "") for c in OUT_COLS})
                    f.flush()
                    ok += 1
                    log(f"    ✓ {r.get('抓取状态')} | 参保 {r.get('参保人数') or '-'} | "
                        f"国标 {r.get('国标行业小类') or '-'} | 企查查 {r.get('企查查行业小类') or '-'} | "
                        f"规模 {r.get('企业规模') or '-'}")
                    if a["probe"]:
                        dump.append(f"### {name}\n字段结果：\n" +
                                    "\n".join(f"  {k} = {r.get(k,'')}" for k in OUT_COLS) +
                                    f"\n\n页面原文前 3000 字：\n{body_head[:3000]}\n")
                    pace()
            except KeyboardInterrupt:
                log("\n⚠ 手动中断，已抓数据全部保存。")

        log(f"\n✅ 完成：成功 {ok} 家，失败/跳过 {skipped} 家 -> {out}")

        if a["probe"] and dump:
            pd = HERE / "probe_dump.txt"
            pd.write_text("\n\n".join(dump), encoding="utf-8")
            log(f"🔍 校准文件已生成：{pd}")
            log("   请把这个文件的内容发给我，我据此校准字段提取规则。")

        if not a["auto"]:
            wait_enter_or_timeout(">>> 按回车关闭浏览器（20秒后自动退出）... ")
        ctx.close()


if __name__ == "__main__":
    main()
