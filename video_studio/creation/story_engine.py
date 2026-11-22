"""本地剧本生成引擎（确定性规则，零外部依赖，零版权风险）。

这不是调用大模型的"AI"，而是一套可用的本地叙事规则引擎：把用户给的梗概
拆解为角色/地点/冲突要素，按经典起承转合结构生成结构化分集正文。产出严格遵循
script.py 的可拆镜格式（场景：/ 动作行 / 角色：台词 / 空行分节拍），
因此能真正驱动后续的拆镜→提示词→提交→挑片→成片全链路，而不是一行占位文字。

设计约束：
- 所有文本模板为原创通用句式，不引用任何具体作品/角色/在世作者风格。
- 生成是确定性的：同一输入必得同一输出，便于测试与复现。
- 用户梗概里的专名（人名/地名）被提取后原样使用，不臆造受保护形象。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


def _stable_int(text: str) -> int:
    """跨进程稳定的整数指纹。

    Python 内置 hash(str) 带随机盐，同一文本在不同进程会得到不同值，
    导致"确定性生成"名不副实（重启服务后主角/地点会变）。这里改用
    md5 前 4 字节，保证同一输入在任何进程都得同一结果。
    """
    return int.from_bytes(hashlib.md5((text or "").encode("utf-8")).digest()[:4], "big")


# --- 通用叙事模板（全部原创）-------------------------------------------------

# 集标题后缀：按叙事阶段给出不同气质
_TITLE_PHASES = [
    "序章·风起", "暗流·初现", "抉择·分岔", "试探·交锋", "真相·渐近",
    "危机·逼近", "反转·心声", "决战·前夕", "余波·回声", "终章·归处",
]

# 每集开场动作描写模板（{loc} 地点、{hero} 主角）
_OPENING_ACTIONS = [
    "晨光落在{loc}，{hero}独自停留，像在等一个不会来的答案。",
    "{loc}人声渐远，{hero}把一桩心事压回心底，转身走入人群。",
    "夜色漫过{loc}，{hero}翻开旧物，熟悉的细节让记忆翻涌。",
    "{loc}的喧闹里，{hero}停下脚步，一个意外的身影闯入视线。",
    "风穿过{loc}，{hero}攥紧手中的东西，做出一个艰难的决定。",
]

# 冲突推进动作模板
_CONFLICT_ACTIONS = [
    "{hero}不肯退让，话到嘴边却咽了回去，气氛骤然紧绷。",
    "一个隐藏的破绽被揭开，在场的人都不敢先开口。",
    "{hero}一步步逼近真相，却发现线索指向了最意想不到的人。",
    "旧日的承诺被重新提起，{hero}陷入两难。",
    "沉默被打破，压了许久的秘密终于摊在桌面上。",
]

# 对白模板：{hero} 主角、{other} 对手/伙伴
_DIALOGUES = [
    ("{hero}", "我以为时间能冲淡一切，可它偏偏记得清清楚楚。"),
    ("{other}", "有些事，你越想躲，它越会找上门。"),
    ("{hero}", "如果当初知道是这样的结局，我还会不会走这一步？"),
    ("{other}", "现在说这些没用，能做的只有往前走。"),
    ("{hero}", "这一次，我不想再错过了。"),
    ("{other}", "你确定？想清楚了就别回头。"),
]

# 悬念钩子模板
_HOOKS = [
    "{hero}没有回头，身后的门却在这时被人轻轻推开。",
    "答案就写在纸的背面，只是此刻还没有人翻过来。",
    "远处传来一声熟悉的呼唤，{hero}猛地站住。",
    "所有人都以为事情结束了，只有{hero}知道真正的开始才刚到。",
    "一只手伸过来，递上的正是那件本该早已消失的东西。",
]

# 地点候选（原创通用场景）
_LOCATIONS = ["江边码头", "旧书店", "雨夜的街角", "城郊车站", "老宅庭院", "临街茶馆", "深夜便利店", "海边栈道"]

# 角色名候选（当用户梗概里没有可识别人名时使用）
_HERO_NAMES = ["林远", "苏晚", "陈默", "江遇", "沈知"]
_OTHER_NAMES = ["故人", "陌生人", "老友", "神秘人", "引路人"]

# 显式命名后常紧跟动词（如「苏晚救了…」），三字候选末字命中这些动词首字时截断为两字
_TRAILING_VERB_CHARS = "救爱嫁娶回走说看是被让跟向又也就都却仍再更很才刚已曾将要会能可把打听问答"

# 题材关键词 → 兜底地点，避免都市题材被配上水边/古镇地点
_URBAN_HINTS = ("总裁", "公司", "白领", "酒店", "宴会", "豪门", "财阀", "千金", "少爷", "写字楼", "商场", "公寓", "总裁夫人")
_RURAL_HINTS = ("山村", "村里", "农田", "老家", "小镇", "乡村", "山寨")
_URBAN_LOCATION = "城郊酒店"
_RURAL_LOCATION = "小镇老街"


@dataclass
class StoryElements:
    """从梗概里抽取的叙事要素。"""
    hero: str
    other: str
    location: str
    theme: str
    synopsis: str


# 身份/关系/时间等非人名词：命中即不当作人名（作为子串判断，如「男主母」）
_NON_NAME_SUBSTR = (
    "总裁", "白月光", "男主", "女主", "主角", "服务生", "母亲", "父亲", "故人",
    "旅人", "医生", "护士", "新娘", "新郎", "保安", "司仪", "宾客", "小姐",
    "先生", "夫人", "阿姨", "少爷", "十年", "三年", "一年", "此时", "此刻",
    "后来", "原来", "从此", "于是", "然而", "真相", "合同", "婚礼", "秘密",
)


def _extract_elements(synopsis: str) -> StoryElements:
    """从梗概提取主角/对手/地点/主题。

    按可信度从高到低识别人名：显式命名（女主X/名叫X）→ 关系称呼（故人X）→
    动词宾语（遇见X/救了X）→ 动词主语（X回到/X救了）。都识别不到才用候选兜底。
    这样用户梗概里写的主角名会真正出现在正文里，而不是被无关的默认值顶替。
    """
    text = (synopsis or "").strip()
    if not text:
        text = "一个普通人回到旧地，寻找失散的故人。"

    def _tidy_name(s: str) -> str:
        """去掉三字候选末尾粘上的动词首字：「苏晚救」→「苏晚」。"""
        if len(s) == 3 and s[-1] in _TRAILING_VERB_CHARS:
            return s[:2]
        return s

    def _valid_name(s: str) -> bool:
        if not re.fullmatch(r"[\u4e00-\u9fa5]{2,3}", s):
            return False
        if any(c in s for c in "年后前时个些候来过去了的和与及在是这那每们"):
            return False
        if any(w in s for w in _NON_NAME_SUBSTR):
            return False
        return True

    def _collect(pattern: str) -> list[str]:
        out = []
        for raw in re.findall(pattern, text):
            n = _tidy_name(raw)
            if _valid_name(n) and n not in out:
                out.append(n)
        return out

    # 1) 显式命名：女主苏晚 / 男主叫傅斯年 / 名叫林远 / 主角陈默
    names = _collect(r"(?:女主|男主|主角|女主角|男主角|她叫|他叫|名叫|名为|叫做)\s*([\u4e00-\u9fa5]{2,3})")
    # 2) 关系称呼 + 人名：故人林远 / 恋人苏晚
    if not names:
        names = _collect(r"(?:故人|朋友|恋人|妻子|丈夫|女儿|儿子|小姐|少爷)\s*([\u4e00-\u9fa5]{2,3})")
    # 3) 动词宾语：遇见苏晚 / 救了林远
    if not names:
        names = _collect(r"(?:寻找|遇见|找到|拜访|重逢|救了|救下|爱上|嫁给|娶了)([\u4e00-\u9fa5]{2,3})")
    # 4) 动词主语：林远回到 / 苏晚救了
    if not names:
        names = _collect(r"([\u4e00-\u9fa5]{2,3})(?=回到|走进|登场|出现|来到|救了|救下|爱上)")

    hero = names[0] if names else _HERO_NAMES[_stable_int(text) % len(_HERO_NAMES)]

    # 地点：命中已知候选 → 通用地点后缀提取 → 按题材兜底
    location = next((loc for loc in _LOCATIONS if loc in text), "")
    if not location:
        m = re.search(r"([\u4e00-\u9fa5]{2,6})(?:酒店|大厅|宴会厅|宅|别墅|公司|医院|车站|咖啡馆|码头|校园|街道|酒吧|公寓|办公室)", text)
        location = m.group(1) if m else ""
    if not location:
        if any(w in text for w in _URBAN_HINTS):
            location = _URBAN_LOCATION
        elif any(w in text for w in _RURAL_HINTS):
            location = _RURAL_LOCATION
        else:
            location = _LOCATIONS[_stable_int(text) % len(_LOCATIONS)]

    # 主题：取梗概前 24 字作为主题线索（清理空白）
    theme = re.sub(r"\s+", "", text)[:24]

    # 对手/另一方：第二个明确人名，或通用称呼
    other = names[1] if len(names) > 1 else _OTHER_NAMES[_stable_int(theme) % len(_OTHER_NAMES)]
    return StoryElements(hero=hero, other=other, location=location, theme=theme, synopsis=text)


def _phase_title(idx: int, total: int, theme: str) -> str:
    """按集序号在整体中的位置给出阶段化标题。"""
    if total <= 1:
        phase = _TITLE_PHASES[0]
    else:
        pos = int((idx - 1) / total * len(_TITLE_PHASES))
        phase = _TITLE_PHASES[min(pos, len(_TITLE_PHASES) - 1)]
    return f"第{idx}集 {phase}"


def generate_episode_body(elements: StoryElements, idx: int, total: int) -> str:
    """生成一集的结构化正文（可被 parse_txt 拆成多个镜头节拍）。"""
    h, o, loc = elements.hero, elements.other, elements.location
    opening = _OPENING_ACTIONS[idx % len(_OPENING_ACTIONS)].format(loc=loc, hero=h)
    conflict = _CONFLICT_ACTIONS[idx % len(_CONFLICT_ACTIONS)].format(hero=h)
    hook = _HOOKS[idx % len(_HOOKS)].format(hero=h)

    # 本集对白：挑两句，角色轮流
    d1 = _DIALOGUES[(idx * 2) % len(_DIALOGUES)]
    d2 = _DIALOGUES[(idx * 2 + 1) % len(_DIALOGUES)]
    line1 = d1[0].format(hero=h, other=o)
    text1 = d1[1].format(hero=h, other=o)
    line2 = d2[0].format(hero=h, other=o)
    text2 = d2[1].format(hero=h, other=o)

    title = _phase_title(idx, total, elements.theme)
    # 严格空行分节拍；场景行以「场景：」开头；对白用「角色：台词」
    return (
        f"{title}\n"
        f"场景：{loc}\n"
        f"{opening}\n\n"
        f"场景：{loc}·内\n"
        f"{conflict}\n"
        f"{line1}：{text1}\n"
        f"{line2}：{text2}\n\n"
        f"场景：{loc}·外\n"
        f"{hook}"
    )


def generate_outlines(synopsis: str, episodes: int) -> list[str]:
    """根据梗概生成 N 集结构化分集正文。"""
    episodes = max(1, int(episodes or 1))
    elements = _extract_elements(synopsis)
    return [generate_episode_body(elements, i, episodes) for i in range(1, episodes + 1)]


# --- 改写 --------------------------------------------------------------------

def rewrite_text(text: str, instruction: str) -> str:
    """按指令对正文做真实改写（本地规则，保留原意）。

    支持：精简 / 扩写 / 对白化 / 悬念强化 / 口语化；未识别指令时做规范化排版。
    """
    inst = (instruction or "").strip()
    lines = [ln.rstrip() for ln in (text or "").splitlines()]
    nonempty = [ln for ln in lines if ln.strip()]

    if any(k in inst for k in ("精简", "简洁", "缩写", "凝练")):
        # 保留场景行 + 每场景首句叙述 + 对白，去掉空行与冗余动作
        out = []
        for ln in nonempty:
            if ln.startswith("场景：") or "：" in ln and not ln.startswith("场景"):
                out.append(ln)
            elif len(out) < len(nonempty):
                out.append(ln[:60])
        return "\n".join(out[:12])

    if any(k in inst for k in ("扩写", "详细", "丰富", "增加细节")):
        out = []
        for ln in nonempty:
            out.append(ln)
            if ln and not ln.startswith("场景：") and "：" not in ln and len(ln) < 80:
                out.append(f"{ln.rstrip('。')}，细节在光与影之间缓缓铺开。")
        return "\n".join(out)

    if any(k in inst for k in ("对白", "对话", "台词")):
        # 把叙述句转成对白：以主角口吻重述
        out = []
        for ln in nonempty:
            if ln.startswith("场景：") or "：" in ln:
                out.append(ln)
            else:
                out.append(f"旁白：{ln}")
        return "\n".join(out)

    if any(k in inst for k in ("悬念", "钩子", "紧张", "反转")):
        out = list(nonempty)
        out.append("")
        out.append("（镜头定格，一束光刚好照亮那封未曾拆开的信。）")
        return "\n".join(out)

    if any(k in inst for k in ("口语", "通俗", "生活化")):
        out = []
        for ln in nonempty:
            rep = ln.replace("，像在", "，好像在").replace("骤然", "一下子").replace("攥紧", "握紧")
            out.append(rep)
        return "\n".join(out)

    if any(k in inst for k in ("幽默", "风趣", "搞笑", "轻松")):
        out = []
        for ln in nonempty:
            if ln.startswith("场景："):
                out.append(ln)
            elif "：" in ln:
                out.append(ln + "（说完自己先笑了）")
            else:
                out.append(ln.rstrip("。") + "，偏偏这时还出了点小状况。")
        return "\n".join(out)

    if any(k in inst for k in ("正式", "严肃", "庄重")):
        out = [ln.replace("一下子", "骤然").replace("握紧", "攥紧") for ln in nonempty]
        return "\n".join(out)

    # 默认：规范化排版，并以指令为注记落一款可追溯的改写痕迹
    cleaned = "\n".join(nonempty)
    return f"{cleaned}\n[改写方向：{inst or '规范化'}]" if inst else cleaned


def continue_script(context: str, n: int) -> str:
    """基于上下文生成承接续写段落。"""
    elements = _extract_elements(context)
    n = max(1, int(n or 1))
    parts = []
    for k in range(1, n + 1):
        hook = _HOOKS[k % len(_HOOKS)].format(hero=elements.hero)
        conflict = _CONFLICT_ACTIONS[k % len(_CONFLICT_ACTIONS)].format(hero=elements.hero)
        parts.append(
            f"场景：{elements.location}·续\n"
            f"{conflict}\n"
            f"{elements.hero}：我们还没到说结束的时候。\n\n"
            f"场景：{elements.location}·外\n"
            f"{hook}"
        )
    return "\n\n".join(parts)
