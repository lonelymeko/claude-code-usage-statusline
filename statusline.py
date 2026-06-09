#!/usr/bin/env python3
"""Claude Code statusLine:实时显示 context 占用 / token 用量 / 套餐额度 / 会话花费。

- ctx / token:解析当前会话 transcript(本地、瞬时)。
- 套餐额度:读 ~/.claude/usage-cache.json;缓存超 TTL 时后台异步刷新
  (调用 /usage 同款接口 GET /api/oauth/usage),渲染永不阻塞。
用法:
  默认            渲染状态栏(读 stdin JSON)
  --refresh       后台模式:打接口写缓存(由脚本自己 fork 调用)
"""
import glob
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
CRED = os.path.join(HOME, ".claude", ".credentials.json")
CACHE = os.path.join(HOME, ".claude", "usage-cache.json")
SELF = os.path.abspath(__file__)
TTL = 60  # 秒;额度缓存有效期

# ---------- 语言 ----------
# 显示语言。留空 "" = 自动读系统语言(LC_ALL/LC_MESSAGES/LANG)。
# 想强制:把它设为 "zh" / "en" / "ja",或设环境变量 CC_STATUSLINE_LANG。
LANG_OVERRIDE = ""

# 5 小时窗口重置信息的显示方式:
#   "clock"     重置时刻(本机时区),如 ↻15:50      ← 默认
#   "countdown" 距重置倒计时,如 ↻3h
#   "both"      两者,如 ↻15:50(3h)
#   "off"       不显示
RESET_STYLE = "clock"

# 进度条格数:上下文 / 额度段的 █░ 进度条宽度(想更长/更短改这里)
BAR_W = 8

# ---------- 可选段开关(常量设默认;可用环境变量 CC_STATUSLINE_<NAME>=1/0 覆盖) ----------
def _on(name, default):
    v = os.environ.get("CC_STATUSLINE_" + name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")

SHOW_MODE   = _on("MODE",   True)    # #8  model 后 effort/thinking/output_style 字形簇
SHOW_WT     = _on("WT",     True)    # #12 worktree 徽章(仅 linked worktree 内出现)
SHOW_NET    = _on("NET",    True)    # #10A 净改动量 ✎+N -N(纯 stdin)
SHOW_TOOLS  = _on("TOOLS",  False)   # #10B 工具混合(扫 transcript,按 block.id 去重)
SHOW_HEALTH = _on("HEALTH", False)   # #10C 工具失败健康点(最近 N 次窗口)
SHOW_TURN   = _on("TURN",   False)   # #10D 上回合时延(user→assistant)
SHOW_GIT    = _on("GIT",    True)    # #11 git 分支+脏文件(后台缓存,渲染非阻塞)
GIT_TTL     = 15                      # git 缓存有效期(秒)
FAIL_WINDOW = 50                      # #10C 仅统计最近 N 次 tool_result
WRAP        = _on("WRAP", True)       # 行太宽时按终端宽度(COLUMNS)自动换行
WRAP_WIDTH  = 0                       # 0=自动读 COLUMNS;>0 则强制按该宽度换行

I18N = {
    "zh":    {"ctx_rem": "(剩{rem:.0f}%)",       "win": "{label}剩{rem:.0f}%",      "soon": "即将"},
    "zh-TW": {"ctx_rem": "(剩{rem:.0f}%)",       "win": "{label}剩{rem:.0f}%",      "soon": "即將"},
    "en":    {"ctx_rem": "({rem:.0f}% left)",    "win": "{label} {rem:.0f}% left",  "soon": "soon"},
    "ja":    {"ctx_rem": "(残{rem:.0f}%)",       "win": "{label}残{rem:.0f}%",      "soon": "間もなく"},
    "ko":    {"ctx_rem": "({rem:.0f}% 남음)",    "win": "{label} {rem:.0f}% 남음",  "soon": "곧"},
    "es":    {"ctx_rem": "({rem:.0f}% rest.)",   "win": "{label} {rem:.0f}% rest.", "soon": "pronto"},
    "fr":    {"ctx_rem": "({rem:.0f}% rest.)",   "win": "{label} {rem:.0f}% rest.", "soon": "bientôt"},
    "de":    {"ctx_rem": "({rem:.0f}% übrig)",   "win": "{label} {rem:.0f}% übrig", "soon": "bald"},
    "pt":    {"ctx_rem": "({rem:.0f}% rest.)",   "win": "{label} {rem:.0f}% rest.", "soon": "em breve"},
    "ru":    {"ctx_rem": "(ост. {rem:.0f}%)",    "win": "{label} ост. {rem:.0f}%",  "soon": "скоро"},
}

def detect_lang():
    v = os.environ.get("CC_STATUSLINE_LANG") or LANG_OVERRIDE
    if not v:
        for e in ("LC_ALL", "LC_MESSAGES", "LANG"):
            if os.environ.get(e):
                v = os.environ[e]
                break
    v = (v or "en").lower().replace("_", "-")
    if v.startswith("zh"):
        if any(x in v for x in ("tw", "hk", "mo", "hant")):
            return "zh-TW"
        return "zh"
    for code in ("ja", "ko", "es", "fr", "de", "pt", "ru"):
        if v.startswith(code):
            return code
    return "en"

LANG = detect_lang()
T = I18N[LANG]

# ---------- ANSI ----------
def c(code, s):
    return f"\033[{code}m{s}\033[0m"

DIM, RED, GREEN, YELLOW, CYAN = "2", "31", "32", "33", "36"

def human(n):
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)

_ANSI = re.compile(r"\033\[[0-9;]*m")

def vis_width(s):
    """段落的终端可见宽度:剥掉 ANSI;宽字符(CJK/emoji)算 2,组合字符算 0。"""
    w = 0
    for ch in _ANSI.sub("", s):
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w

def wrap_segments(parts, width):
    """把段落按 width 贪心打包成多行(同行段间 ` | ` 占 3 宽),返回 list[list]。"""
    lines, cur, cur_w = [], [], 0
    for p in parts:
        pw = vis_width(p)
        add = pw + (3 if cur else 0)       # 3 = " | " 的可见宽度
        if cur and cur_w + add > width:
            lines.append(cur)
            cur, cur_w = [p], pw
        else:
            cur.append(p)
            cur_w += add
    if cur:
        lines.append(cur)
    return lines

def remain_color(rem):
    if rem <= 15:
        return RED
    if rem <= 40:
        return YELLOW
    return GREEN

def bar(pct, fill):
    """按百分比生成定宽进度条:填充段用 fill 色 █,剩余段暗显 ░。"""
    pct = max(0.0, min(100.0, pct))
    n = max(0, min(BAR_W, round(pct / 100 * BAR_W)))
    return c(fill, "█" * n) + c(DIM, "░" * (BAR_W - n))

def usage_window(label, rem, rst):
    """渲染单个额度窗(5h/7d):`5h ███████░ 93% ↻15:50`,bar 填充=剩余%。"""
    col = remain_color(rem)
    s = c(col, f"{label} ") + bar(rem, col) + c(col, f" {rem:.0f}%")
    if rst:
        s += " " + c(DIM, f"↻{rst}")
    return s

# ---------- 凭证:文件优先,macOS 回退 Keychain ----------
def read_access_token():
    """读取 Claude OAuth accessToken。

    优先读 ~/.claude/.credentials.json;该文件不存在时回退到 macOS 登录钥匙串
    ——macOS 上 Claude Code 默认把凭证存进 Keychain(条目名
    "Claude Code-credentials")而非该文件,否则套餐额度段在 mac 上永远不显示。
    其它平台无此文件时维持原样抛错(由 refresh_usage 静默兜底)。
    """
    try:
        with open(CRED) as f:
            return json.load(f)["claudeAiOauth"]["accessToken"]
    except FileNotFoundError:
        if sys.platform != "darwin":
            raise
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            raise RuntimeError("keychain lookup failed")
        return json.loads(out.stdout)["claudeAiOauth"]["accessToken"]

# ---------- 额度:后台刷新 ----------
def refresh_usage():
    try:
        tok = read_access_token()
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {tok}",
                "anthropic-beta": "oauth-2025-04-20",
                "anthropic-version": "2023-06-01",
                "User-Agent": "claude-cli/statusline",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        data["_fetched_at"] = time.time()
        tmp = CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, CACHE)
    except Exception:
        # 失败(如 token 过期)就保留旧缓存,不报错
        pass

def maybe_spawn_refresh():
    fresh = False
    try:
        fresh = (time.time() - os.path.getmtime(CACHE)) < TTL
    except OSError:
        fresh = False
    if not fresh:
        try:
            subprocess.Popen(
                [sys.executable, SELF, "--refresh"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass

def _countdown(mins):
    if mins <= 0:
        return T["soon"]
    if mins < 60:
        return f"{mins}m"
    h, m = divmod(mins, 60)
    if h < 24:
        return f"{h}h{m}m" if m else f"{h}h"
    d, hh = divmod(h, 24)
    return f"{d}d{hh}h" if hh else f"{d}d"  # 两位精度:天→时 / 时→分 / 分

_WD_EN = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

def friendly_dt(local):
    """友好的绝对重置时间:今天只给时刻;明天/后天/本周内给相对日;更远给日期。"""
    diff = (local.date() - datetime.now().astimezone().date()).days
    hm = local.strftime("%H:%M")
    if diff <= 0:
        return hm
    if diff > 6:
        return local.strftime("%m-%d ") + hm  # 超过一周回退到日期
    if LANG.startswith("zh"):
        tw = LANG == "zh-TW"
        if diff == 1:
            return f"明天 {hm}"
        if diff == 2:
            return f"{'後天' if tw else '后天'} {hm}"
        wd = ("週" if tw else "周") + "一二三四五六日"[local.weekday()]
        return f"{wd} {hm}"
    return f"{_WD_EN[local.weekday()]} {hm}"

def fmt_reset(iso):
    if RESET_STYLE == "off":
        return ""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        mins = int((t - datetime.now(timezone.utc)).total_seconds() // 60)
        clock = friendly_dt(t.astimezone())  # 友好相对日 + 时刻
        if RESET_STYLE == "countdown":
            return _countdown(mins)
        if RESET_STYLE == "both":
            return f"{clock}({_countdown(mins)})"
        return clock  # "clock"(默认)
    except Exception:
        return ""

def reset_countdown(iso):
    """距该窗重置还剩多久(窗口剩余时长):45m / 2h / 5d3h / 即将。无法解析返回 ''。"""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        mins = int((t - datetime.now(timezone.utc)).total_seconds() // 60)
        return _countdown(mins)
    except Exception:
        return ""

def usage_segment():
    maybe_spawn_refresh()
    try:
        with open(CACHE) as f:
            u = json.load(f)
    except Exception:
        return None  # 还没缓存(首次渲染),下次就有了
    out = []
    fh = u.get("five_hour") or {}
    sd = u.get("seven_day") or {}
    if fh.get("utilization") is not None:
        rem = 100 - fh["utilization"]
        iso = fh.get("resets_at", "")
        out.append(usage_window(reset_countdown(iso) or "5h", rem, fmt_reset(iso)))
    if sd.get("utilization") is not None:
        rem = 100 - sd["utilization"]
        iso = sd.get("resets_at", "")
        out.append(usage_window(reset_countdown(iso) or "7d", rem, fmt_reset(iso)))
    return "  ".join(out) if out else None  # 双空格分隔两窗,留足间隙

# ---------- context:解析 transcript ----------
def ctx_tokens(transcript):
    used = out = 0
    if transcript and os.path.exists(transcript):
        try:
            last = None
            with open(transcript, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        usage = (json.loads(line).get("message") or {}).get("usage")
                    except Exception:
                        continue
                    if usage:
                        last = usage
            if last:
                used = (last.get("input_tokens", 0) or 0) \
                     + (last.get("cache_read_input_tokens", 0) or 0) \
                     + (last.get("cache_creation_input_tokens", 0) or 0)
                out = last.get("output_tokens", 0) or 0
        except Exception:
            pass
    return used, out

# ---------- 通用工具 ----------
def _write_json_atomic(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception:
        pass

def _parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

# ---------- #8 模式字形簇(effort / thinking / output_style) ----------
_EFFORT = {"low": "lo", "high": "hi", "xhigh": "xh", "max": "mx"}  # medium 默认隐藏

def mode_suffix(data):
    """model 芯片后缀:仅非默认时出现,默认态返回空串(不改芯片一个字节)。"""
    g = _EFFORT.get(((data.get("effort") or {}).get("level") or "").lower(), "")
    if (data.get("thinking") or {}).get("enabled"):
        g += "⁝"                                  # 思考开:窄字形 tricolon
    style = (data.get("output_style") or {}).get("name")
    if style and style != "default":
        g += style[0].lower()                      # 非默认输出风格:首字母
    return c(DIM, " " + g) if g else ""

# ---------- #12 worktree 徽章 ----------
def worktree_segment(data):
    wt = ((data.get("workspace") or {}).get("git_worktree") or "").strip()
    if not wt:
        return None                                # 主树无此字段 → 不渲染
    if len(wt) > 24:
        wt = wt[:23] + "…"
    return c(DIM, f"⑂ {wt}")

# ---------- #10A 净改动量 ----------
def netlines_segment(cost_obj):
    add = cost_obj.get("total_lines_added")
    rem = cost_obj.get("total_lines_removed")
    if not isinstance(add, (int, float)) or not isinstance(rem, (int, float)):
        return None
    if add == 0 and rem == 0:
        return None
    return c(GREEN, f"✎+{int(add)}") + " " + c(RED, f"-{int(rem)}")

# ---------- #10 B/C/D 会话级 transcript 度量(单遍,仅在需要时调用) ----------
def session_metrics(transcript, want_tools, want_health, want_turn):
    tools, results, last_delta, pending_user = {}, [], None, None
    if not (transcript and os.path.exists(transcript)):
        return {}
    try:
        with open(transcript, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                msg = o.get("message") or {}
                content = msg.get("content")
                role = msg.get("role") or o.get("type")
                if isinstance(content, list) and (want_tools or want_health):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "tool_use" and want_tools:
                            tools.setdefault(b.get("name") or "?", set()).add(b.get("id"))
                        elif bt == "tool_result" and want_health:
                            results.append(b.get("is_error") is True)  # None 视为非失败
                if want_turn:
                    ts = _parse_ts(o.get("timestamp") or "")
                    if ts is None:
                        continue
                    is_human = role == "user" and (
                        isinstance(content, str)
                        or (isinstance(content, list)
                            and not any(isinstance(b, dict) and b.get("type") == "tool_result"
                                        for b in content)))
                    if is_human:
                        pending_user = ts
                    elif role == "assistant" and pending_user is not None:
                        last_delta = ts - pending_user
                        pending_user = None
    except Exception:
        return {}
    return {
        "tools": {k: len(v) for k, v in tools.items()},
        "results": results[-FAIL_WINDOW:],
        "last_turn": last_delta,
    }

def tool_mix_segment(tools):
    if not tools:
        return None
    top = sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    body = "·".join(f"{name[:3]}{cnt}" for name, cnt in top)
    return c(DIM, "▸" + body + ("…" if len(tools) > 3 else ""))

def fail_health_segment(results):
    n = len(results)
    if n == 0:
        return None
    fails = sum(1 for x in results if x)
    rate = fails / n * 100
    col = RED if rate >= 20 else (YELLOW if rate >= 5 else GREEN)
    return c(col, f"●{rate:.0f}% ({fails}/{n})")

def last_turn_segment(secs):
    if secs is None or secs < 0:
        return None
    txt = f"{int(secs)}s" if secs < 60 else _countdown(int(secs // 60))
    return c(DIM, f"⏱{txt}")

# ---------- #11 git 分支 + 脏文件(后台缓存,渲染非阻塞) ----------
def git_cache_path(sid):
    return os.path.join(HOME, ".claude", f"git-cache-{sid}.json")

def _gc_git_caches():
    cutoff = time.time() - 7 * 86400
    for p in glob.glob(os.path.join(HOME, ".claude", "git-cache-*.json")):
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass

def git_refresh(cwd, sid):
    """后台进程:跑 git 命令,把结果原子写进 per-session 缓存。永不阻塞渲染。"""
    info = {"is_repo": False, "_fetched_at": time.time()}
    try:
        br = subprocess.run(["git", "-C", cwd, "symbolic-ref", "--short", "-q", "HEAD"],
                            capture_output=True, text=True, timeout=5)
        if br.returncode == 0:
            branch = br.stdout.strip()
        else:  # detached HEAD
            sh = subprocess.run(["git", "-C", cwd, "rev-parse", "--short", "HEAD"],
                                capture_output=True, text=True, timeout=5)
            if sh.returncode != 0:
                _write_json_atomic(git_cache_path(sid), info)  # 非 git 仓库
                _gc_git_caches()
                return
            branch = "@" + sh.stdout.strip()
        st = subprocess.run(["git", "-C", cwd, "status", "--porcelain"],
                            capture_output=True, text=True, timeout=5)
        dirty = sum(1 for ln in st.stdout.splitlines() if ln.strip()) if st.returncode == 0 else 0
        info.update(is_repo=True, branch=branch, dirty=dirty)
    except Exception:
        pass
    _write_json_atomic(git_cache_path(sid), info)
    _gc_git_caches()

def maybe_spawn_git_refresh(cwd, sid):
    try:
        fresh = (time.time() - os.path.getmtime(git_cache_path(sid))) < GIT_TTL
    except OSError:
        fresh = False
    if not fresh:
        try:
            subprocess.Popen([sys.executable, SELF, "--git-refresh", cwd, sid],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        except Exception:
            pass

def git_segment(cwd, sid):
    if not cwd or not sid:
        return None
    maybe_spawn_git_refresh(cwd, sid)
    try:
        with open(git_cache_path(sid)) as f:
            g = json.load(f)
    except Exception:
        return None                                # 首帧无缓存,下帧就有
    if not g.get("is_repo"):
        return None
    branch = g.get("branch") or "?"
    if len(branch) > 24:
        branch = branch[:23] + "…"
    s = c(CYAN, f"⎇ {branch}")
    if g.get("dirty", 0) > 0:
        s += " " + c(YELLOW, f"●{g['dirty']}")
    return s

# ---------- 主渲染 ----------
def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--refresh":
        refresh_usage()
        return
    if len(sys.argv) > 3 and sys.argv[1] == "--git-refresh":
        git_refresh(sys.argv[2], sys.argv[3])
        return

    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    if os.environ.get("CC_STATUSLINE_DEBUG"):  # 调试:dump 真实 stdin 便于核对字段
        try:
            with open(os.path.join(HOME, ".claude", "statusline-last-stdin.json"), "w") as _f:
                json.dump(data, _f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    model = (data.get("model") or {}).get("display_name") or "Claude"
    cost_obj = data.get("cost") or {}
    cost = cost_obj.get("total_cost_usd")
    ctx_max = 1_000_000 if data.get("exceeds_200k_tokens") else 200_000
    used, out = ctx_tokens(data.get("transcript_path") or "")

    pct = (used / ctx_max * 100) if ctx_max else 0
    ctx_col = RED if pct >= 85 else (YELLOW if pct >= 60 else GREEN)

    parts = [c(CYAN, f"⚡{model}") + (mode_suffix(data) if SHOW_MODE else "")]
    if SHOW_WT:
        wt = worktree_segment(data)
        if wt:
            parts.append(wt)
    parts.append(c(ctx_col, f"ctx {human(used)}/{human(ctx_max)} ") + bar(pct, ctx_col) + c(ctx_col, f" {pct:.0f}%"))
    if used or out:
        parts.append(c(DIM, f"⬆ {human(used)}  ⬇ {human(out)}"))
    seg = usage_segment()
    if seg:
        parts.append(seg)
    if cost is not None:
        parts.append(c(GREEN, f"${cost:.3f}"))
    if SHOW_NET:
        nl = netlines_segment(cost_obj)
        if nl:
            parts.append(nl)
    if SHOW_TOOLS or SHOW_HEALTH or SHOW_TURN:
        m = session_metrics(data.get("transcript_path") or "", SHOW_TOOLS, SHOW_HEALTH, SHOW_TURN)
        if SHOW_TOOLS:
            s = tool_mix_segment(m.get("tools") or {})
            if s:
                parts.append(s)
        if SHOW_HEALTH:
            s = fail_health_segment(m.get("results") or [])
            if s:
                parts.append(s)
        if SHOW_TURN:
            s = last_turn_segment(m.get("last_turn"))
            if s:
                parts.append(s)
    if SHOW_GIT:
        gs = git_segment(data.get("cwd") or (data.get("workspace") or {}).get("current_dir"),
                         data.get("session_id"))
        if gs:
            parts.append(gs)

    sep = c(DIM, " | ")
    width = WRAP_WIDTH
    if width <= 0:
        try:
            width = int(os.environ.get("COLUMNS", "0"))
        except ValueError:
            width = 0
        if width <= 0:
            width = 100                     # COLUMNS 不可用时的兜底宽度
    if WRAP:
        lines = wrap_segments(parts, max(20, width - 1))
        print("\n".join(sep.join(ln) for ln in lines))
    else:
        print(sep.join(parts))

if __name__ == "__main__":
    main()
