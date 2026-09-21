"""Built-in plugin: Daily Task Management with intelligent execution suggestions."""

import json
import logging
import os
import random
import uuid
from datetime import datetime
from html import escape
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets

from app_paths import APP_ROOT
from app_plugins.api import MAIN_MENU, PluginCommand

DEFAULT_DAILY_TASKS_FILE = APP_ROOT / "DailyTasks.json"

# Urgency identifiers
URGENCY_HOURS = "hours"
URGENCY_TODAY = "today"
URGENCY_DAYS = "days"

URGENCY_LEVELS = {
    URGENCY_HOURS: {
        "key": URGENCY_HOURS,
        "label": "数小时内（紧急，今日必死线）",
        "short_label": "数小时内",
        "tier": 3,
        "base_score": 1000.0,
        "color": "#D32F2F",
    },
    URGENCY_TODAY: {
        "key": URGENCY_TODAY,
        "label": "今日内（当天完成）",
        "short_label": "今日内",
        "tier": 2,
        "base_score": 500.0,
        "color": "#E65100",
    },
    URGENCY_DAYS: {
        "key": URGENCY_DAYS,
        "label": "数日内（常规/近期）",
        "short_label": "数日内",
        "tier": 1,
        "base_score": 100.0,
        "color": "#1976D2",
    },
}

# Difficulty identifiers
DIFFICULTY_EASY = "easy"
DIFFICULTY_MEDIUM = "medium"
DIFFICULTY_HARD = "hard"

DIFFICULTY_LEVELS = {
    DIFFICULTY_EASY: {
        "key": DIFFICULTY_EASY,
        "label": "简单（轻松无痛）",
        "short_label": "简单",
        "score": 1,
        "color": "#2E7D32",
    },
    DIFFICULTY_MEDIUM: {
        "key": DIFFICULTY_MEDIUM,
        "label": "中等（适度烧脑）",
        "short_label": "中等",
        "score": 2,
        "color": "#F57C00",
    },
    DIFFICULTY_HARD: {
        "key": DIFFICULTY_HARD,
        "label": "困难（硬核大Boss）",
        "short_label": "困难",
        "score": 3,
        "color": "#7B1FA2",
    },
}


def normalize_urgency(value):
    """Normalize arbitrary urgency string into standard urgency keys."""
    if not value:
        return URGENCY_TODAY
    val = str(value).strip().lower()
    import re

    # Direct key matches
    if val in (URGENCY_HOURS, URGENCY_TODAY, URGENCY_DAYS):
        return val

    # Immediate / hourly deadlines (minutes, seconds, hours, 紧急)
    if any(k in val for k in ("hour", "小时", "极急", "火急", "紧急", "urgent", "critical", "半小时", "分钟", "min", "sec", "秒")):
        return URGENCY_HOURS
    if re.search(r"\b\d+\s*(h|hr|hrs)\b", val) or re.search(r"^\d+h$", val):
        return URGENCY_HOURS

    # Today deadlines
    if any(k in val for k in ("today", "今日", "当天", "今天", "半天")):
        return URGENCY_TODAY

    # Days / long term
    if any(k in val for k in ("day", "天", "日", "week", "周", "近期", "常规", "以后", "长期", "later")):
        nums = re.findall(r"\d+", val)
        if nums:
            n = int(nums[0])
            if n == 0:
                return URGENCY_HOURS
            if n == 1:
                return URGENCY_TODAY
            return URGENCY_DAYS
        return URGENCY_DAYS
    return URGENCY_TODAY


def normalize_difficulty(value):
    """Normalize arbitrary difficulty string into standard difficulty keys."""
    if not value:
        return DIFFICULTY_MEDIUM
    val = str(value).strip().lower()
    import re

    # Direct key matches
    if val in (DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_HARD):
        return val

    # Easy phrases or explicit negations of difficulty (e.g., 不难, 毫无难度)
    if any(k in val for k in ("不难", "没难度", "毫无难度", "低", "简单", "轻松", "easy", "simple")):
        return DIFFICULTY_EASY

    # Medium phrases (evaluated before '烧脑' so '中等（适度烧脑）' correctly maps to medium)
    if any(k in val for k in ("中等", "适度", "普通", "一般", "平缓", "medium", "moderate")):
        return DIFFICULTY_MEDIUM

    # Check numeric rating (1 = easy, 2 = medium, >=3 = hard)
    nums = re.findall(r"\d+", val)
    if nums:
        n = int(nums[0])
        if n == 1:
            return DIFFICULTY_EASY
        if n == 2:
            return DIFFICULTY_MEDIUM
        return DIFFICULTY_HARD

    # Hard keywords
    if any(k in val for k in ("hard", "困难", "硬核", "烧脑", "极难", "大boss", "难", "高", "difficult")):
        return DIFFICULTY_HARD

    return DIFFICULTY_MEDIUM


# Humorous commentary pools
TASK_COMMENTARIES = {
    (URGENCY_HOURS, DIFFICULTY_HARD): [
        "🔥 警报拉响！十万火急还要命的硬核任务。建议先泡一杯特浓咖啡，拔掉网线直接冲！搞定它你今天就是神！",
        "🚨 火烧眉毛预警！既难又急，这是今天的终极大Boss。别追求过度完美了，先弄出个能跑的版本救命要紧！",
        "⚡ 渡劫时刻！既然注定逃不掉，不如闭眼硬刚。早超生早解脱，干完直接下班都不带心虚的！",
        "💣 核弹级任务！死线就在眼前，脑细胞必须全力燃烧。别切屏看手机了，速战速决！",
    ],
    (URGENCY_HOURS, DIFFICULTY_MEDIUM): [
        "⏱️ 倒计时滴答响！中等难度但催得很急。集中火力30分钟拿下，千万别被消息弹窗切断心流！",
        "🎯 战术突击！这个任务难度适中但死线压头，速战速决，给自己争取宝贵的摸鱼喘息时间！",
        "🏃 冲刺模式！趁现在脑细胞正兴奋，一口气推平它，不给拖延症留任何余地！",
        "⏰ 火线救急！难度刚好能驾驭，时间刻不容缓，深吸一口气立刻开工！",
    ],
    (URGENCY_HOURS, DIFFICULTY_EASY): [
        "⚡ 顺手捏死！小菜一碟还催得急，2分钟搞定，瞬间收获虚假但极其解压的成就感！",
        "🍰 降维打击！又急又简单，点两下鼠标就没了，赶紧清掉免得在心头一直烦你！",
        "💨 闪电战！送分题必须先秒，既能交差又能让列表进度条往前猛窜一大截！",
        "🧼 顺手抹平！难度为零但死线逼近，随手一挥直接消灭，毫无痛感！",
    ],
    (URGENCY_TODAY, DIFFICULTY_HARD): [
        "🐸 今日丑青蛙！今天最耗脑子的一块硬骨头。趁意志力电池还是满格，先吃了它，剩下的全是弟弟！",
        "🏋️ 力量举时间！中度紧急但难度拉满。建议放首战歌，把它拆成3个小步骤慢慢啃下来！",
        "🧗 登顶决战！今天的核心主线大挑战，拿下它今天就算及格甚至优秀了，冲！",
        "🥊 巅峰对决！硬茬子一个，但也正是展现技术的时候。搞定它今天就可以宣告胜利！",
    ],
    (URGENCY_TODAY, DIFFICULTY_MEDIUM): [
        "🍱 今日主菜！难度温和，节奏正好。按部就班推进，千万别拖到下午变成'数小时内'！",
        "☕ 咖啡伴侣！中规中矩的标准任务，听两首歌的时间搞定，稳扎稳打绝不翻车！",
        "🚗 巡航模式！难度适中，稳步驾驶，完成一个就离下班摸鱼更进一步！",
        "🍲 招牌例汤！今天必做清单里的营养担当，平稳输出，30分钟内优雅解决！",
    ],
    (URGENCY_TODAY, DIFFICULTY_EASY): [
        "🍬 精神软糖！轻松愉快毫无心理负担，随时顺手消灭，给任务列表画个漂亮的绿色对勾！",
        "🎈 轻松小点心！做这个就像喝温水一样丝滑，快速消灭，心情舒畅！",
        "🪴 摸鱼过渡！难度几乎为零，趁着状态好快速清理，毫无痛感甚至有点想笑！",
        "🎯 信心充值包！5分钟搞定，给今天的工作开一个行云流水的好头！",
    ],
    (URGENCY_DAYS, DIFFICULTY_HARD): [
        "🧊 潜伏冰山！虽然还有几天，但难度不可小觑。今天可以先看眼文档列个提纲，谨防最后一天通宵补作业！",
        "🐉 沉睡巨龙！远期大难题，今天有余力就瞄一眼，没空先别惊动它，别被它吓退！",
        "🧱 长期基石！不急但难，属于高价值战略储备，有空就搬两块砖，没空就放着候补！",
        "🔭 远景工程！硬核任务，先在脑子里建个模，今天不必死磕，给大脑一点发酵时间！",
    ],
    (URGENCY_DAYS, DIFFICULTY_MEDIUM): [
        "📦 备选物资！过两天才需要，今天排在最后候补。前面的急件清完要是闲得慌再翻牌子！",
        "💤 随缘队列！中等难度，放着也不会过期，今天先全力处理眼前火烧眉毛的事！",
        "🏖️ 候补梯队！安全距离内的小任务，不急于一时，按自己节奏来即可！",
        "🛋️ 慢工细活！今天不必抢跑，等主线任务清爽了再来慢慢调理！",
    ],
    (URGENCY_DAYS, DIFFICULTY_EASY): [
        "🧘 佛系躺平！既不急又简单，简直是上天赐予的摸鱼护身符。留到最后无痛收尾！",
        "🎮 休闲保留地！留着快下班或大脑宕机时做，完全不费脑子，轻松拿捏！",
        "🦥 树懒节奏！优先级殿底但毫无压力，今天顺手做了是缘分，不做明天依然岁月静好！",
        "🍵 随缘清汤！完全没有紧迫感，什么时候做都来得及，放平心态享受当下！",
    ],
}

STRATEGIES = [
    {
        "name": "🔥 紧急优先 + 先吃青蛙流",
        "tag": "eat_frog",
        "desc": "以绝对死线为最高指引；同等紧急时先啃最硬的骨头，趁意志力电量满格打赢硬仗！",
    },
    {
        "name": "🎯 紧急优先 + 快速启动流",
        "tag": "quick_wins",
        "desc": "优先救火守住死线；同等紧急时先挑软柿子捏，2分钟清掉简单任务，信心暴涨！",
    },
    {
        "name": "⚡ 战术机动 + 节奏平衡流",
        "tag": "balanced",
        "desc": "死线底线寸步不让；同等紧急时动静结合，在烧脑攻坚与无痛摸鱼中优雅穿梭！",
    },
]


def generate_execution_suggestion(tasks, seed=None):
    """Sort pending tasks logically by urgency and difficulty with humor and randomness.

    The algorithm guarantees that urgency tiers strictly dominate:
      Hours (Tier 3) > Today (Tier 2) > Days (Tier 1).
    Within each urgency tier, strategies and randomized factors vary the order
    and provide fresh humorous commentary.
    """
    rng = random.Random(seed)

    # Filter for pending tasks safely
    if not isinstance(tasks, (list, tuple, set)):
        pending = []
    else:
        pending = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            comp = t.get("completed", False)
            if isinstance(comp, str):
                is_comp = comp.strip().lower() in ("true", "1", "yes")
            else:
                is_comp = bool(comp)
            if not is_comp:
                pending.append(dict(t))

    # Edge case 1: No pending tasks
    if not pending:
        commentaries = [
            "🎉 诊断报告：【无事一身轻 / 摸鱼大圆满】！任务列表空空如也，恭喜你已超脱三界！此时不去喝杯手冲咖啡或者看窗外发呆，都对不起这绝世好时光！",
            "🌟 诊断报告：【天下太平 / 躺平大师】！目前没有任何待办任务！你是全宇宙最高效的打工人，请保持姿势，优雅享受无罪恶感的摸鱼时刻！",
            "🍵 诊断报告：【清闲自得 / 摸鱼境界全开】！零待办清单达成了！如果领导路过，请务必紧皱眉头快速敲击键盘，假装在运行千亿级计算！",
        ]
        return {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now().isoformat(),
            "title": f"执行建议 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "strategy_name": "☕ 纯享摸鱼流（无待办）",
            "strategy_desc": "任务清零，天下无事，宜喝水、放空、晒太阳。",
            "overall_commentary": rng.choice(commentaries),
            "recommended_order": [],
            "total_pending": 0,
        }

    # Edge case 2: Exactly 1 pending task
    if len(pending) == 1:
        task = pending[0]
        urgency = normalize_urgency(task.get("urgency"))
        difficulty = normalize_difficulty(task.get("difficulty"))
        single_comments = [
            "🎯 独苗出列！整个列表就这一个独苗，还犹豫啥？闭着眼一顿操作把它灭了，今天就圆满交工！",
            "🥊 孤军奋战！就这一件事挡在你的下班路上，干掉它，你就能毫无心理负担地提前思考晚餐吃什么！",
            "⚡ 集中歼灭！没有选择困难症的烦恼，专注当下，搞定即可立刻开启无敌自由时光！",
        ]
        single_diagnoses = [
            "今日诊断：【极简主义日】仅有 1 个待办任务。消灭它，自由就是你的！",
            "今日诊断：【定点清除模式】全场仅剩最后 1 个独苗待办。速战速决，提前享受自由人生！",
            "今日诊断：【下班倒计时】距离今天彻底收工只差最后 1 步，冲刺搞定它，开香槟庆祝！",
            "今日诊断：【清爽极简】列表空前清爽，消灭眼前唯一的阻碍，直通完美收工！",
        ]
        return {
            "id": uuid.uuid4().hex,
            "created_at": datetime.now().isoformat(),
            "title": f"执行建议 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "strategy_name": "🎯 精准狙击流（唯一目标）",
            "strategy_desc": "专心消灭眼前唯一的阻碍，直通收工。",
            "overall_commentary": rng.choice(single_diagnoses),
            "recommended_order": [
                {
                    "rank": 1,
                    "task_id": str(task.get("id", "")),
                    "title": str(task.get("title") or "").strip() or "未命名任务",
                    "urgency": urgency,
                    "urgency_label": URGENCY_LEVELS[urgency]["short_label"],
                    "difficulty": difficulty,
                    "difficulty_label": DIFFICULTY_LEVELS[difficulty]["short_label"],
                    "humorous_comment": rng.choice(single_comments),
                    "score": 1000.0,
                }
            ],
            "total_pending": 1,
        }

    # Multiple tasks
    strategy = rng.choice(STRATEGIES)

    scored_tasks = []
    for t in pending:
        urgency = normalize_urgency(t.get("urgency"))
        difficulty = normalize_difficulty(t.get("difficulty"))
        base_score = URGENCY_LEVELS[urgency]["base_score"]
        diff_score = DIFFICULTY_LEVELS[difficulty]["score"]

        # Intra-tier bonus depending on strategy
        if strategy["tag"] == "eat_frog":
            intra_bonus = diff_score * 25.0
        elif strategy["tag"] == "quick_wins":
            intra_bonus = (4 - diff_score) * 25.0
        else:
            intra_bonus = diff_score * 12.0

        # Jitter strictly within [-10, 10] to never breach the tier gap (>=400)
        jitter = rng.uniform(-10.0, 10.0)
        final_score = base_score + intra_bonus + jitter
        scored_tasks.append((final_score, t, urgency, difficulty))

    # Sort descending by score
    scored_tasks.sort(key=lambda item: item[0], reverse=True)

    recommended_order = []
    for rank, (score, t, urgency, difficulty) in enumerate(scored_tasks, start=1):
        candidates = TASK_COMMENTARIES.get(
            (urgency, difficulty),
            ["稳定推进，按步就班！"],
        )
        comment = rng.choice(candidates)
        if rank == 1 and urgency == URGENCY_HOURS:
            comment = "👑【头号通缉令】" + comment
        elif rank == len(scored_tasks) and urgency == URGENCY_DAYS:
            comment = "🛌【压箱底法宝】" + comment

        recommended_order.append({
            "rank": rank,
            "task_id": str(t.get("id", "")),
            "title": str(t.get("title") or "").strip() or "未命名任务",
            "urgency": urgency,
            "urgency_label": URGENCY_LEVELS[urgency]["short_label"],
            "difficulty": difficulty,
            "difficulty_label": DIFFICULTY_LEVELS[difficulty]["short_label"],
            "humorous_comment": comment,
            "score": round(score, 2),
        })

    # Overall humorous diagnosis
    urgent_count = sum(1 for _, _, u, _ in scored_tasks if u == URGENCY_HOURS)
    hard_count = sum(1 for _, _, _, d in scored_tasks if d == DIFFICULTY_HARD)
    easy_count = sum(1 for _, _, _, d in scored_tasks if d == DIFFICULTY_EASY)
    total = len(scored_tasks)

    if urgent_count >= 2 or (urgent_count >= 1 and hard_count >= 2):
        diagnoses = [
            f"🚨 今日诊断：【特级战备 / 火烧眉毛综合征】！列表中有 {urgent_count} 个极度紧急任务，外加 {hard_count} 个硬核大茬！建议戴上降噪耳机，泡好双倍浓缩美式，进入无情干活机器模式！谁来聊天都假装在深度思考！",
            f"🔥 今日诊断：【救火队长紧急集合】！{urgent_count} 个任务死线就在眼前！这时候别纠结代码排版或文字修辞了，先做出一个能跑的救命再说，冲！",
        ]
    elif hard_count / total >= 0.5:
        diagnoses = [
            f"🥊 今日诊断：【硬核渡劫 / 意志力大考】！硬骨头占比高达 {round(hard_count / total * 100)}%！今天注定是要燃烧脑细胞的日子，干完这一票你就是全办公室最硬的狠人，晚上必须吃顿好的犒劳自己！",
            f"🧗 今日诊断：【勇攀高峰 / 青蛙盛宴】！满眼看去全是大项目。按照建议一个一个拆解击破，只要吃掉前两只丑青蛙，后面就会豁然开朗！",
        ]
    elif easy_count / total >= 0.6:
        diagnoses = [
            f"🍰 今日诊断：【无痛摸鱼 / 快乐划水日】！简单任务占比超过 {round(easy_count / total * 100)}%！今天简直如春风拂面，随便敲几下键盘就能画上一堆勾，甚至有充足的时间构思下午茶！",
            f"🎈 今日诊断：【轻松拿捏日】！送分题扎堆出现，难度温和。保持微笑，顺水推舟就能高效完成，体面又轻松！",
        ]
    else:
        diagnoses = [
            f"✨ 今日诊断：【节奏大师 / 黄金平衡期】！共 {total} 项待办，紧急与常规交织，难易相得益彰。只要控制住刷短视频的手，今天绝对能优雅按时打卡下班！",
            f"🎯 今日诊断：【稳扎稳打日】！任务结构合理健康，先急后缓、动静结合。跟着这份建议执行，下班时你不仅能完成任务，大脑依然神清气爽！",
        ]

    return {
        "id": uuid.uuid4().hex,
        "created_at": datetime.now().isoformat(),
        "title": f"执行建议 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "strategy_name": strategy["name"],
        "strategy_desc": strategy["desc"],
        "overall_commentary": rng.choice(diagnoses),
        "recommended_order": recommended_order,
        "total_pending": total,
    }


class DailyTaskStore:
    """Persistent storage manager for daily tasks and saved execution suggestions."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else DEFAULT_DAILY_TASKS_FILE
        self._data = {
            "version": 1,
            "tasks": [],
            "saved_suggestions": [],
        }
        self.load()

    def load(self):
        if not self.path.exists():
            return self._data
        try:
            content = self.path.read_text(encoding="utf-8-sig")
            if not content.strip():
                return self._data
            raw = json.loads(content)
            if not isinstance(raw, dict):
                return self._data
            self._data["version"] = raw.get("version", 1)

            tasks = []
            raw_tasks = raw.get("tasks")
            if not isinstance(raw_tasks, (list, tuple)):
                raw_tasks = []

            for t in raw_tasks:
                if not isinstance(t, dict):
                    continue
                title = str(t.get("title") or "").strip()
                if not title:
                    continue
                urgency = normalize_urgency(t.get("urgency"))
                difficulty = normalize_difficulty(t.get("difficulty"))
                task_id = str(t.get("id") or uuid.uuid4().hex)
                comp = t.get("completed", False)
                if isinstance(comp, str):
                    is_comp = comp.strip().lower() in ("true", "1", "yes")
                else:
                    is_comp = bool(comp)
                tasks.append({
                    "id": task_id,
                    "title": title,
                    "urgency": urgency,
                    "urgency_label": URGENCY_LEVELS[urgency]["label"],
                    "difficulty": difficulty,
                    "difficulty_label": DIFFICULTY_LEVELS[difficulty]["label"],
                    "description": str(t.get("description") or "").strip(),
                    "completed": is_comp,
                    "created_at": str(
                        t.get("created_at") or datetime.now().isoformat()
                    ),
                    "updated_at": str(
                        t.get("updated_at") or datetime.now().isoformat()
                    ),
                })
            self._data["tasks"] = tasks

            suggestions = []
            raw_sug = raw.get("saved_suggestions")
            if not isinstance(raw_sug, (list, tuple)):
                raw_sug = []

            for s in raw_sug:
                if not isinstance(s, dict):
                    continue
                s_id = str(s.get("id") or uuid.uuid4().hex)
                rec_order = s.get("recommended_order")
                if not isinstance(rec_order, (list, tuple)):
                    rec_order = []

                total_p = s.get("total_pending")
                try:
                    total_count = (
                        int(total_p)
                        if total_p is not None and str(total_p).strip() != ""
                        else len(rec_order)
                    )
                except (TypeError, ValueError):
                    total_count = len(rec_order)

                suggestions.append({
                    "id": s_id,
                    "title": str(s.get("title") or "未命名建议"),
                    "created_at": str(
                        s.get("created_at") or datetime.now().isoformat()
                    ),
                    "strategy_name": str(s.get("strategy_name") or ""),
                    "strategy_desc": str(s.get("strategy_desc") or ""),
                    "overall_commentary": str(s.get("overall_commentary") or ""),
                    "recommended_order": list(rec_order),
                    "total_pending": total_count,
                })
            self._data["saved_suggestions"] = suggestions
        except Exception as e:
            try:
                import shutil
                bak_name = f"{self.path.name}.corrupted.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
                bak_path = self.path.with_name(bak_name)
                shutil.copy2(str(self.path), str(bak_path))
                logging.error(f"每日任务数据文件异常，已自动备份至 {bak_name}：{e}")
            except Exception:
                pass
            logging.error(f"加载每日任务数据失败：{e}")
        return self._data

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            temp_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(temp_path), str(self.path))
            return True
        except Exception as e:
            logging.error(f"保存每日任务数据失败：{e}")
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            return False

    def _mutate(self, action_fn):
        """Execute an in-memory mutation with atomic rollback if save fails."""
        import copy
        backup = copy.deepcopy(self._data)
        try:
            result = action_fn()
            if not self.save():
                self._data = backup
                raise OSError(
                    f"保存每日任务数据失败，文件可能被其他程序占用或缺少写入权限：{self.path}"
                )
            return result
        except Exception:
            self._data = backup
            raise

    def get_tasks(self, filter_mode="all"):
        if filter_mode == "pending":
            return [t for t in self._data["tasks"] if not t.get("completed")]
        elif filter_mode == "completed":
            return [t for t in self._data["tasks"] if t.get("completed")]
        return list(self._data["tasks"])

    def get_task(self, task_id):
        for t in self._data["tasks"]:
            if t["id"] == task_id:
                return t
        return None

    def add_task(
        self,
        title,
        urgency="today",
        difficulty="medium",
        description="",
    ):
        clean_title = str(title or "").strip()
        if not clean_title:
            raise ValueError("任务标题不能为空")
        urgency = normalize_urgency(urgency)
        difficulty = normalize_difficulty(difficulty)
        now_str = datetime.now().isoformat()
        task = {
            "id": uuid.uuid4().hex,
            "title": clean_title,
            "urgency": urgency,
            "urgency_label": URGENCY_LEVELS[urgency]["label"],
            "difficulty": difficulty,
            "difficulty_label": DIFFICULTY_LEVELS[difficulty]["label"],
            "description": str(description or "").strip(),
            "completed": False,
            "created_at": now_str,
            "updated_at": now_str,
        }

        def _do():
            self._data["tasks"].append(task)
            return task

        return self._mutate(_do)

    def update_task(
        self,
        task_id,
        title=None,
        urgency=None,
        difficulty=None,
        description=None,
        completed=None,
    ):
        task = self.get_task(task_id)
        if not task:
            raise KeyError(f"未找到任务：{task_id}")
        if title is not None:
            clean_title = str(title).strip()
            if not clean_title:
                raise ValueError("任务标题不能为空")

        def _do():
            t = self.get_task(task_id)
            if title is not None:
                t["title"] = str(title).strip()
            if urgency is not None:
                norm_u = normalize_urgency(urgency)
                t["urgency"] = norm_u
                t["urgency_label"] = URGENCY_LEVELS[norm_u]["label"]
            if difficulty is not None:
                norm_d = normalize_difficulty(difficulty)
                t["difficulty"] = norm_d
                t["difficulty_label"] = DIFFICULTY_LEVELS[norm_d]["label"]
            if description is not None:
                t["description"] = str(description).strip()
            if completed is not None:
                t["completed"] = bool(completed)
            t["updated_at"] = datetime.now().isoformat()
            return t

        return self._mutate(_do)

    def delete_task(self, task_id):
        before_len = len(self._data["tasks"])
        target = [t for t in self._data["tasks"] if t["id"] != task_id]
        if len(target) == before_len:
            return False

        def _do():
            self._data["tasks"] = [
                t for t in self._data["tasks"] if t["id"] != task_id
            ]
            return True

        return self._mutate(_do)

    def toggle_completed(self, task_id):
        task = self.get_task(task_id)
        if not task:
            raise KeyError(f"未找到任务：{task_id}")

        def _do():
            t = self.get_task(task_id)
            t["completed"] = not t.get("completed", False)
            t["updated_at"] = datetime.now().isoformat()
            return t

        return self._mutate(_do)

    def save_suggestion(self, suggestion):
        if not isinstance(suggestion, dict):
            raise TypeError("建议数据必须是字典")
        s = json.loads(json.dumps(suggestion))
        existing_ids = {
            item["id"]
            for item in self._data.get("saved_suggestions", [])
            if isinstance(item, dict) and "id" in item
        }
        if not s.get("id") or s["id"] in existing_ids:
            s["id"] = uuid.uuid4().hex
        if not s.get("created_at"):
            s["created_at"] = datetime.now().isoformat()

        def _do():
            self._data["saved_suggestions"].insert(0, s)
            return s

        return self._mutate(_do)

    def get_saved_suggestions(self):
        return list(self._data["saved_suggestions"])

    def delete_suggestion(self, suggestion_id):
        before_len = len(self._data["saved_suggestions"])
        target = [
            s
            for s in self._data["saved_suggestions"]
            if s["id"] != suggestion_id
        ]
        if len(target) == before_len:
            return False

        def _do():
            self._data["saved_suggestions"] = [
                s
                for s in self._data["saved_suggestions"]
                if s["id"] != suggestion_id
            ]
            return True

        return self._mutate(_do)


class TaskEditDialog(QtWidgets.QDialog):
    """Modal dialog for creating and editing individual tasks."""

    def __init__(self, task=None, parent=None):
        super().__init__(parent)
        self.task = task
        self.setWindowTitle("编辑任务" if task else "新增任务")
        self.resize(480, 320)
        self.setModal(True)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        form = QtWidgets.QFormLayout()
        form.setLabelAlignment(QtCore.Qt.AlignRight)

        self.title_edit = QtWidgets.QLineEdit(self)
        self.title_edit.setPlaceholderText("请输入任务名称（必填）")
        form.addRow("任务名称 *：", self.title_edit)

        self.urgency_combo = QtWidgets.QComboBox(self)
        for key in (URGENCY_HOURS, URGENCY_TODAY, URGENCY_DAYS):
            info = URGENCY_LEVELS[key]
            self.urgency_combo.addItem(info["label"], key)
        form.addRow("紧迫程度：", self.urgency_combo)

        self.difficulty_combo = QtWidgets.QComboBox(self)
        for key in (DIFFICULTY_EASY, DIFFICULTY_MEDIUM, DIFFICULTY_HARD):
            info = DIFFICULTY_LEVELS[key]
            self.difficulty_combo.addItem(info["label"], key)
        form.addRow("难度级别：", self.difficulty_combo)

        self.desc_edit = QtWidgets.QTextEdit(self)
        self.desc_edit.setPlaceholderText(
            "可选，输入任务描述、执行背景或备忘提示"
        )
        self.desc_edit.setMaximumHeight(90)
        form.addRow("备注说明：", self.desc_edit)

        layout.addLayout(form)

        if task:
            self.title_edit.setText(task.get("title", ""))
            curr_u = normalize_urgency(task.get("urgency"))
            idx_u = self.urgency_combo.findData(curr_u)
            if idx_u >= 0:
                self.urgency_combo.setCurrentIndex(idx_u)
            curr_d = normalize_difficulty(task.get("difficulty"))
            idx_d = self.difficulty_combo.findData(curr_d)
            if idx_d >= 0:
                self.difficulty_combo.setCurrentIndex(idx_d)
            self.desc_edit.setPlainText(task.get("description", ""))
        else:
            idx_u = self.urgency_combo.findData(URGENCY_TODAY)
            if idx_u >= 0:
                self.urgency_combo.setCurrentIndex(idx_u)
            idx_d = self.difficulty_combo.findData(DIFFICULTY_MEDIUM)
            if idx_d >= 0:
                self.difficulty_combo.setCurrentIndex(idx_d)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel,
            QtCore.Qt.Horizontal,
            self,
        )
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText("确定")
        buttons.button(QtWidgets.QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def validate_and_accept(self):
        title = self.title_edit.text().strip()
        if not title:
            QtWidgets.QMessageBox.warning(self, "输入错误", "任务名称不能为空！")
            self.title_edit.setFocus()
            return
        self.accept()

    def get_data(self):
        return {
            "title": self.title_edit.text().strip(),
            "urgency": self.urgency_combo.currentData(),
            "difficulty": self.difficulty_combo.currentData(),
            "description": self.desc_edit.toPlainText().strip(),
        }


def render_suggestion_html(suggestion):
    """Render a suggestion dictionary into an elegant HTML report."""
    if not isinstance(suggestion, dict):
        return "<div style='padding:20px; color:#999; text-align:center;'>无可用的建议内容</div>"
    title = escape(str(suggestion.get("title") or "执行建议"))
    strategy = escape(str(suggestion.get("strategy_name") or "默认策略"))
    strategy_desc = escape(str(suggestion.get("strategy_desc") or ""))
    diag = escape(str(suggestion.get("overall_commentary") or ""))
    date_str = escape(
        str(suggestion.get("created_at") or "")[:19].replace("T", " ")
    )

    rows_html = []
    order_items = suggestion.get("recommended_order")
    if not isinstance(order_items, (list, tuple)):
        order_items = []
    for item in order_items:
        if not isinstance(item, dict):
            continue
        rank = escape(str(item.get("rank") if item.get("rank") is not None else ""))
        t_title = escape(str(item.get("title") or "未命名任务"))
        u_key = normalize_urgency(item.get("urgency"))
        d_key = normalize_difficulty(item.get("difficulty"))
        u_label = escape(
            str(item.get("urgency_label") or URGENCY_LEVELS[u_key]["short_label"])
        )
        d_label = escape(
            str(item.get("difficulty_label") or DIFFICULTY_LEVELS[d_key]["short_label"])
        )
        comment = escape(
            str(item.get("humorous_comment") or "稳定推进，按步就班！")
        )

        u_color = URGENCY_LEVELS.get(u_key, {}).get("color", "#666")
        d_color = DIFFICULTY_LEVELS.get(d_key, {}).get("color", "#666")

        rows_html.append(f"""
            <tr style="border-bottom: 1px solid #EEEEEE;">
                <td style="padding: 10px 8px; font-weight: bold; color: #1976D2; font-size: 14px;">#{rank}</td>
                <td style="padding: 10px 8px; font-weight: 600; color: #212121;">{t_title}</td>
                <td style="padding: 10px 8px;"><span style="color: {u_color}; font-weight: bold;">{u_label}</span></td>
                <td style="padding: 10px 8px;"><span style="color: {d_color}; font-weight: bold;">{d_label}</span></td>
                <td style="padding: 10px 8px; color: #37474F; line-height: 1.4;">{comment}</td>
            </tr>
        """)

    table_content = "".join(rows_html) if rows_html else """
        <tr>
            <td colspan="5" style="padding: 20px; text-align: center; color: #757575;">暂无待办任务，尽情享受清闲时光！</td>
        </tr>
    """

    return f"""
    <div style="font-family: 'Segoe UI', Microsoft YaHei, sans-serif; padding: 12px; color: #212121;">
        <h2 style="margin: 0 0 6px 0; color: #1565C0;">{title}</h2>
        <div style="color: #757575; font-size: 12px; margin-bottom: 14px;">生成时间：{date_str}</div>

        <div style="background-color: #E3F2FD; border-left: 5px solid #1976D2; padding: 12px; margin-bottom: 12px; border-radius: 4px;">
            <strong style="color: #0D47A1; font-size: 14px;">【执行策略】{strategy}</strong><br/>
            <div style="color: #455A64; font-size: 13px; margin-top: 4px;">{strategy_desc}</div>
        </div>

        <div style="background-color: #FFF8E1; border-left: 5px solid #FFA000; padding: 12px; margin-bottom: 16px; border-radius: 4px;">
            <strong style="color: #E65100; font-size: 14px;">【今日诊断】</strong><br/>
            <div style="color: #3E2723; font-size: 13px; margin-top: 4px; line-height: 1.5;">{diag}</div>
        </div>

        <h3 style="color: #37474F; margin: 16px 0 8px 0;">📋 建议执行顺位表：</h3>
        <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
            <thead>
                <tr style="background-color: #ECEFF1; border-bottom: 2px solid #CFD8DC; text-align: left;">
                    <th style="padding: 8px; width: 45px;">顺位</th>
                    <th style="padding: 8px; width: 160px;">任务名称</th>
                    <th style="padding: 8px; width: 85px;">紧迫度</th>
                    <th style="padding: 8px; width: 75px;">难度</th>
                    <th style="padding: 8px;">智能建议锦囊与幽默点评</th>
                </tr>
            </thead>
            <tbody>
                {table_content}
            </tbody>
        </table>
    </div>
    """


class DailyTasksDialog(QtWidgets.QDialog):
    """Non-modal main window for daily tasks and intelligent execution suggestions."""

    def __init__(self, store=None, parent=None):
        super().__init__(parent)
        self.store = store if store is not None else DailyTaskStore()
        self.current_suggestion = None

        self.setWindowTitle("每日任务管理与智能建议")
        self.resize(960, 640)
        self.setModal(False)
        self.setWindowModality(QtCore.Qt.NonModal)

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)

        # Header banner
        header_layout = QtWidgets.QHBoxLayout()
        header_vbox = QtWidgets.QVBoxLayout()
        title_label = QtWidgets.QLabel("📅 每日任务管理与智能执行建议", self)
        title_label.setStyleSheet(
            "font-size: 16px; font-weight: bold; color: #1565C0;"
        )
        subtitle_label = QtWidgets.QLabel(
            "灵活规划今日任务，一键生成结合科学排序与幽默点评的最优执行建议",
            self,
        )
        subtitle_label.setStyleSheet("color: #616161; font-size: 12px;")
        header_vbox.addWidget(title_label)
        header_vbox.addWidget(subtitle_label)
        header_layout.addLayout(header_vbox)
        header_layout.addStretch(1)
        main_layout.addLayout(header_layout)

        # Tabs
        self.tabs = QtWidgets.QTabWidget(self)
        self.tabs.setStyleSheet("QTabBar::tab { height: 32px; font-weight: bold; }")
        main_layout.addWidget(self.tabs, 1)

        # Tab 1: Tasks
        self._setup_tasks_tab()
        # Tab 2: Suggestion
        self._setup_suggestion_tab()
        # Tab 3: Saved Suggestions
        self._setup_saved_tab()

        # Status footer
        self.status_label = QtWidgets.QLabel("就绪", self)
        self.status_label.setStyleSheet("color: #666; font-size: 12px;")
        main_layout.addWidget(self.status_label)

        # Keyboard shortcuts
        self.del_task_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key_Delete), self.task_table
        )
        self.del_task_shortcut.activated.connect(self.delete_task_action)
        self.edit_task_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key_Return), self.task_table
        )
        self.edit_task_shortcut.activated.connect(self.edit_task_action)
        self.edit_task_enter_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key_Enter), self.task_table
        )
        self.edit_task_enter_shortcut.activated.connect(self.edit_task_action)
        self.toggle_task_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key_Space), self.task_table
        )
        self.toggle_task_shortcut.activated.connect(self.toggle_completed_action)
        self.add_task_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence("Ctrl+N"), self
        )
        self.add_task_shortcut.activated.connect(self.add_task_action)
        self.add_task_ins_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key_Insert), self.task_table
        )
        self.add_task_ins_shortcut.activated.connect(self.add_task_action)

        self.del_saved_shortcut = QtWidgets.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key_Delete), self.saved_list
        )
        self.del_saved_shortcut.activated.connect(self.delete_saved_suggestion_action)

        self.refresh_all()

    def _setup_tasks_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Controls bar
        controls = QtWidgets.QHBoxLayout()
        controls.addWidget(QtWidgets.QLabel("筛选：", tab))
        self.filter_combo = QtWidgets.QComboBox(tab)
        self.filter_combo.addItem("全部任务", "all")
        self.filter_combo.addItem("仅看待办", "pending")
        self.filter_combo.addItem("仅看已完成", "completed")
        self.filter_combo.currentIndexChanged.connect(self.refresh_tasks)
        controls.addWidget(self.filter_combo)

        controls.addSpacing(10)
        self.add_btn = QtWidgets.QPushButton("➕ 新增任务", tab)
        self.edit_btn = QtWidgets.QPushButton("✏️ 编辑任务", tab)
        self.toggle_btn = QtWidgets.QPushButton("✔️ 切换完成状态", tab)
        self.delete_btn = QtWidgets.QPushButton("🗑️ 删除任务", tab)

        self.add_btn.clicked.connect(self.add_task_action)
        self.edit_btn.clicked.connect(self.edit_task_action)
        self.toggle_btn.clicked.connect(self.toggle_completed_action)
        self.delete_btn.clicked.connect(self.delete_task_action)

        controls.addWidget(self.add_btn)
        controls.addWidget(self.edit_btn)
        controls.addWidget(self.toggle_btn)
        controls.addWidget(self.delete_btn)

        controls.addStretch(1)

        self.generate_btn = QtWidgets.QPushButton("✨ 生成执行建议", tab)
        self.generate_btn.setStyleSheet(
            "background-color: #1976D2; color: white; font-weight: bold; "
            "padding: 6px 14px; border-radius: 4px;"
        )
        self.generate_btn.clicked.connect(self.generate_suggestion_action)
        controls.addWidget(self.generate_btn)

        layout.addLayout(controls)

        # Table
        self.task_table = QtWidgets.QTableWidget(tab)
        self.task_table.setColumnCount(6)
        self.task_table.setHorizontalHeaderLabels([
            "状态",
            "任务名称",
            "紧迫程度",
            "难度级别",
            "备注说明",
            "创建时间",
        ])
        self.task_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows
        )
        self.task_table.setSelectionMode(
            QtWidgets.QAbstractItemView.SingleSelection
        )
        self.task_table.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        self.task_table.setAlternatingRowColors(True)
        self.task_table.cellDoubleClicked.connect(self.edit_task_action)

        header = self.task_table.horizontalHeader()
        header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)
        header.setSectionResizeMode(5, QtWidgets.QHeaderView.ResizeToContents)

        self.task_table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.task_table.customContextMenuRequested.connect(
            self._show_task_context_menu
        )

        layout.addWidget(self.task_table, 1)
        self.tabs.addTab(tab, "📋 任务清单")

    def _setup_suggestion_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Diagnostic header banner
        diag_frame = QtWidgets.QFrame(tab)
        diag_frame.setStyleSheet(
            "background-color: #FFF9C4; border: 1px solid #FFF176; border-radius: 4px; padding: 6px;"
        )
        diag_layout = QtWidgets.QVBoxLayout(diag_frame)
        diag_layout.setContentsMargins(6, 6, 6, 6)

        self.strategy_label = QtWidgets.QLabel("尚未生成建议", diag_frame)
        self.strategy_label.setStyleSheet(
            "font-weight: bold; color: #E65100; font-size: 13px;"
        )
        diag_layout.addWidget(self.strategy_label)

        self.diag_browser = QtWidgets.QTextBrowser(diag_frame)
        self.diag_browser.setMaximumHeight(85)
        self.diag_browser.setOpenExternalLinks(True)
        self.diag_browser.setStyleSheet(
            "background: transparent; border: none; color: #37474F; font-size: 12px;"
        )
        diag_layout.addWidget(self.diag_browser)
        layout.addWidget(diag_frame)

        # Action toolbar
        action_bar = QtWidgets.QHBoxLayout()
        self.regen_btn = QtWidgets.QPushButton("🔄 重新生成建议", tab)
        self.regen_btn.clicked.connect(self.generate_suggestion_action)
        self.save_btn = QtWidgets.QPushButton("💾 保存此建议", tab)
        self.save_btn.clicked.connect(self.save_suggestion_action)
        self.save_btn.setEnabled(False)

        action_bar.addWidget(self.regen_btn)
        action_bar.addWidget(self.save_btn)
        action_bar.addSpacing(12)

        tip_label = QtWidgets.QLabel(
            "💡 提示：每次重新生成均结合随机性与幽默算法，带来不同的风趣点评与战术视角。",
            tab,
        )
        tip_label.setStyleSheet("color: #757575; font-size: 11px;")
        action_bar.addWidget(tip_label)
        action_bar.addStretch(1)

        layout.addLayout(action_bar)

        # Suggestion Table
        self.suggestion_table = QtWidgets.QTableWidget(tab)
        self.suggestion_table.setColumnCount(5)
        self.suggestion_table.setHorizontalHeaderLabels([
            "顺位",
            "任务名称",
            "紧迫度",
            "难度",
            "执行锦囊与幽默点评",
        ])
        self.suggestion_table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectRows
        )
        self.suggestion_table.setEditTriggers(
            QtWidgets.QAbstractItemView.NoEditTriggers
        )
        self.suggestion_table.setAlternatingRowColors(True)

        s_header = self.suggestion_table.horizontalHeader()
        s_header.setSectionResizeMode(0, QtWidgets.QHeaderView.ResizeToContents)
        s_header.setSectionResizeMode(1, QtWidgets.QHeaderView.Interactive)
        s_header.resizeSection(1, 180)
        s_header.setSectionResizeMode(2, QtWidgets.QHeaderView.ResizeToContents)
        s_header.setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)
        s_header.setSectionResizeMode(4, QtWidgets.QHeaderView.Stretch)

        layout.addWidget(self.suggestion_table, 1)
        self.tabs.addTab(tab, "💡 智能执行建议")

    def _setup_saved_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(tab)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Left list
        left_layout = QtWidgets.QVBoxLayout()
        left_layout.addWidget(
            QtWidgets.QLabel("历史保存记录（点击查看）：", tab)
        )
        self.saved_list = QtWidgets.QListWidget(tab)
        self.saved_list.currentItemChanged.connect(self._on_saved_item_selected)
        left_layout.addWidget(self.saved_list, 1)

        self.delete_saved_btn = QtWidgets.QPushButton("🗑️ 删除所选建议", tab)
        self.delete_saved_btn.clicked.connect(self.delete_saved_suggestion_action)
        left_layout.addWidget(self.delete_saved_btn)

        layout.addLayout(left_layout, 1)

        # Right browser
        right_layout = QtWidgets.QVBoxLayout()
        right_layout.addWidget(QtWidgets.QLabel("建议详情报告：", tab))
        self.saved_browser = QtWidgets.QTextBrowser(tab)
        self.saved_browser.setOpenExternalLinks(True)
        self.saved_browser.setStyleSheet(
            "background-color: #FAFAFA; border: 1px solid #E0E0E0; border-radius: 4px;"
        )
        self.saved_list.itemClicked.connect(
            lambda item: self._on_saved_item_selected(item, None)
        )
        right_layout.addWidget(self.saved_browser, 3)

        layout.addLayout(right_layout, 3)
        self.tabs.addTab(tab, "📜 已保存的建议")

    def refresh_all(self):
        self.store.load()
        self.refresh_tasks()
        self.refresh_saved_list()

    def refresh_tasks(self, *args):
        prev_row = (
            self.task_table.currentRow()
            if hasattr(self, "task_table")
            else -1
        )
        filter_mode = (
            self.filter_combo.currentData()
            if hasattr(self, "filter_combo")
            else "all"
        )
        tasks = self.store.get_tasks(filter_mode=filter_mode)
        all_tasks = self.store.get_tasks(filter_mode="all")
        pending_tasks = [t for t in all_tasks if not t.get("completed")]
        completed_tasks = [t for t in all_tasks if t.get("completed")]

        self.task_table.setUpdatesEnabled(False)
        try:
            self.task_table.setRowCount(len(tasks))
            for row, t in enumerate(tasks):
                # Status
                completed = t.get("completed", False)
                status_item = QtWidgets.QTableWidgetItem(
                    "✔️ 已完成" if completed else "⏳ 待办"
                )
                status_item.setTextAlignment(QtCore.Qt.AlignCenter)
                if completed:
                    status_item.setForeground(QtGui.QBrush(QtGui.QColor("#2E7D32")))
                else:
                    status_item.setForeground(QtGui.QBrush(QtGui.QColor("#E65100")))
                status_item.setData(QtCore.Qt.UserRole, t["id"])
                self.task_table.setItem(row, 0, status_item)

                # Title
                title_item = QtWidgets.QTableWidgetItem(t.get("title", ""))
                title_item.setToolTip(t.get("title", ""))
                font = title_item.font()
                if completed:
                    font.setStrikeOut(True)
                    title_item.setForeground(QtGui.QBrush(QtGui.QColor("#9E9E9E")))
                else:
                    font.setBold(True)
                title_item.setFont(font)
                self.task_table.setItem(row, 1, title_item)

                # Urgency
                u_key = t.get("urgency", URGENCY_TODAY)
                u_info = URGENCY_LEVELS.get(u_key, URGENCY_LEVELS[URGENCY_TODAY])
                u_item = QtWidgets.QTableWidgetItem(u_info["short_label"])
                u_item.setTextAlignment(QtCore.Qt.AlignCenter)
                u_item.setForeground(QtGui.QBrush(QtGui.QColor(u_info["color"])))
                font_u = u_item.font()
                font_u.setBold(True)
                u_item.setFont(font_u)
                self.task_table.setItem(row, 2, u_item)

                # Difficulty
                d_key = t.get("difficulty", DIFFICULTY_MEDIUM)
                d_info = DIFFICULTY_LEVELS.get(
                    d_key, DIFFICULTY_LEVELS[DIFFICULTY_MEDIUM]
                )
                d_item = QtWidgets.QTableWidgetItem(d_info["short_label"])
                d_item.setTextAlignment(QtCore.Qt.AlignCenter)
                d_item.setForeground(QtGui.QBrush(QtGui.QColor(d_info["color"])))
                font_d = d_item.font()
                font_d.setBold(True)
                d_item.setFont(font_d)
                self.task_table.setItem(row, 3, d_item)

                # Description
                desc_text = t.get("description", "")
                desc_item = QtWidgets.QTableWidgetItem(desc_text)
                desc_item.setToolTip(desc_text)
                self.task_table.setItem(row, 4, desc_item)

                # Created At
                created_str = (
                    t.get("created_at", "")[:16].replace("T", " ")
                    if t.get("created_at")
                    else ""
                )
                time_item = QtWidgets.QTableWidgetItem(created_str)
                time_item.setTextAlignment(QtCore.Qt.AlignCenter)
                self.task_table.setItem(row, 5, time_item)
        finally:
            self.task_table.setUpdatesEnabled(True)

        self.task_table.resizeRowsToContents()

        if self.task_table.rowCount() > 0:
            new_row = min(max(0, prev_row), self.task_table.rowCount() - 1)
            self.task_table.setCurrentCell(new_row, 0)

        saved_count = len(self.store.get_saved_suggestions())
        self.status_label.setText(
            f"共 {len(all_tasks)} 项任务（待办 {len(pending_tasks)}，已完成 {len(completed_tasks)}） | 已保存执行建议 {saved_count} 条"
        )

    def refresh_saved_list(self):
        saved = self.store.get_saved_suggestions()
        curr_row = self.saved_list.currentRow()
        self.saved_list.clear()

        for s in saved:
            title = str(s.get("title") or "未命名建议")
            rec = s.get("recommended_order")
            rec_len = len(rec) if isinstance(rec, (list, tuple)) else 0
            total_p = s.get("total_pending")
            try:
                count = (
                    int(total_p)
                    if total_p is not None and str(total_p).strip() != ""
                    else rec_len
                )
            except (TypeError, ValueError):
                count = rec_len
            if title.startswith("执行建议 - "):
                display_title = title
            else:
                created = str(s.get("created_at") or "")[:16].replace("T", " ")
                display_title = f"{created} - {title}" if created else title
            item = QtWidgets.QListWidgetItem(f"{display_title} ({count}项)")
            item.setData(QtCore.Qt.UserRole, s.get("id"))
            self.saved_list.addItem(item)

        if saved:
            idx = min(max(0, curr_row), len(saved) - 1)
            self.saved_list.setCurrentRow(idx)
        else:
            self.saved_browser.setHtml(
                "<div style='padding:20px; color:#999; text-align:center;'>暂无已保存的建议记录。在【智能执行建议】页点击'保存此建议'即可在此查看。</div>"
            )

    def _selected_task_id(self):
        row = self.task_table.currentRow()
        if row < 0:
            return None
        item = self.task_table.item(row, 0)
        return item.data(QtCore.Qt.UserRole) if item else None

    def add_task_action(self):
        dlg = TaskEditDialog(parent=self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            data = dlg.get_data()
            try:
                self.store.add_task(
                    title=data["title"],
                    urgency=data["urgency"],
                    difficulty=data["difficulty"],
                    description=data["description"],
                )
                if hasattr(self, "filter_combo") and self.filter_combo.currentData() == "completed":
                    self.filter_combo.setCurrentIndex(0)
                else:
                    self.refresh_tasks()
            except Exception as e:
                QtWidgets.QMessageBox.critical(
                    self, "添加失败", f"添加任务出错：{e}"
                )

    def edit_task_action(self, *args):
        if (
            args
            and isinstance(args[0], int)
            and not isinstance(args[0], bool)
            and args[0] >= 0
        ):
            self.task_table.setCurrentCell(args[0], 0)
        task_id = self._selected_task_id()
        if not task_id:
            QtWidgets.QMessageBox.information(
                self, "提示", "请先在列表中选中要编辑的任务。"
            )
            return
        task = self.store.get_task(task_id)
        if not task:
            return
        dlg = TaskEditDialog(task=task, parent=self)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            data = dlg.get_data()
            try:
                self.store.update_task(
                    task_id=task_id,
                    title=data["title"],
                    urgency=data["urgency"],
                    difficulty=data["difficulty"],
                    description=data["description"],
                )
                self.refresh_tasks()
            except Exception as e:
                QtWidgets.QMessageBox.critical(
                    self, "更新失败", f"更新任务出错：{e}"
                )

    def toggle_completed_action(self, *args):
        task_id = self._selected_task_id()
        if not task_id:
            QtWidgets.QMessageBox.information(
                self, "提示", "请先在列表中选中要切换状态的任务。"
            )
            return
        try:
            self.store.toggle_completed(task_id)
            self.refresh_tasks()
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "操作失败", f"切换任务状态出错：{e}"
            )

    def delete_task_action(self, *args):
        task_id = self._selected_task_id()
        if not task_id:
            QtWidgets.QMessageBox.information(
                self, "提示", "请先在列表中选中要删除的任务。"
            )
            return
        task = self.store.get_task(task_id)
        if not task:
            return
        task_title = str(task.get("title") or "未命名任务")
        btn = QtWidgets.QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除任务【{task_title}】吗？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if btn == QtWidgets.QMessageBox.Yes:
            try:
                self.store.delete_task(task_id)
                self.refresh_tasks()
            except Exception as e:
                QtWidgets.QMessageBox.critical(
                    self, "删除失败", f"删除任务出错：{e}"
                )

    def _show_task_context_menu(self, pos):
        clicked_row = self.task_table.rowAt(pos.y())
        if clicked_row >= 0:
            self.task_table.setCurrentCell(clicked_row, 0)
        else:
            self.task_table.setCurrentCell(-1, -1)
            self.task_table.clearSelection()

        menu = QtWidgets.QMenu(self)
        add_act = menu.addAction("➕ 新增任务")
        add_act.triggered.connect(self.add_task_action)

        task_id = self._selected_task_id()
        if task_id:
            edit_act = menu.addAction("✏️ 编辑任务")
            edit_act.triggered.connect(self.edit_task_action)
            toggle_act = menu.addAction("✔️ 切换完成状态")
            toggle_act.triggered.connect(self.toggle_completed_action)
            del_act = menu.addAction("🗑️ 删除任务")
            del_act.triggered.connect(self.delete_task_action)

        menu.addSeparator()
        gen_act = menu.addAction("✨ 生成执行建议")
        gen_act.triggered.connect(self.generate_suggestion_action)
        menu.exec_(self.task_table.viewport().mapToGlobal(pos))

    def generate_suggestion_action(self, *args):
        all_tasks = self.store.get_tasks("all")
        pending_tasks = [t for t in all_tasks if not t.get("completed")]

        # Run intelligent execution suggestion algorithm
        self.current_suggestion = generate_execution_suggestion(pending_tasks)

        # Update Tab 2 UI
        self.strategy_label.setText(
            f"【{self.current_suggestion['strategy_name']}】—— {self.current_suggestion['strategy_desc']}"
        )
        self.diag_browser.setText(self.current_suggestion["overall_commentary"])

        orders = self.current_suggestion.get("recommended_order", [])
        self.suggestion_table.setUpdatesEnabled(False)
        try:
            self.suggestion_table.setRowCount(len(orders))
            for row, item in enumerate(orders):
                # Rank
                r_item = QtWidgets.QTableWidgetItem(f"#{item['rank']}")
                r_item.setTextAlignment(QtCore.Qt.AlignCenter)
                font_r = r_item.font()
                font_r.setBold(True)
                r_item.setFont(font_r)
                r_item.setForeground(QtGui.QBrush(QtGui.QColor("#1976D2")))
                self.suggestion_table.setItem(row, 0, r_item)

                # Title
                t_title = str(item.get("title") or "未命名任务")
                t_item = QtWidgets.QTableWidgetItem(t_title)
                t_item.setToolTip(t_title)
                font_t = t_item.font()
                font_t.setBold(True)
                t_item.setFont(font_t)
                self.suggestion_table.setItem(row, 1, t_item)

                # Urgency
                u_key = item.get("urgency", URGENCY_TODAY)
                u_info = URGENCY_LEVELS.get(u_key, URGENCY_LEVELS[URGENCY_TODAY])
                u_label = str(item.get("urgency_label") or u_info["short_label"])
                u_item = QtWidgets.QTableWidgetItem(u_label)
                u_item.setTextAlignment(QtCore.Qt.AlignCenter)
                u_item.setForeground(QtGui.QBrush(QtGui.QColor(u_info["color"])))
                font_u = u_item.font()
                font_u.setBold(True)
                u_item.setFont(font_u)
                self.suggestion_table.setItem(row, 2, u_item)

                # Difficulty
                d_key = item.get("difficulty", DIFFICULTY_MEDIUM)
                d_info = DIFFICULTY_LEVELS.get(
                    d_key, DIFFICULTY_LEVELS[DIFFICULTY_MEDIUM]
                )
                d_label = str(item.get("difficulty_label") or d_info["short_label"])
                d_item = QtWidgets.QTableWidgetItem(d_label)
                d_item.setTextAlignment(QtCore.Qt.AlignCenter)
                d_item.setForeground(QtGui.QBrush(QtGui.QColor(d_info["color"])))
                font_d = d_item.font()
                font_d.setBold(True)
                d_item.setFont(font_d)
                self.suggestion_table.setItem(row, 3, d_item)

                # Comment
                comment_text = str(
                    item.get("humorous_comment") or "稳定推进，按步就班！"
                )
                c_item = QtWidgets.QTableWidgetItem(comment_text)
                c_item.setToolTip(comment_text)
                self.suggestion_table.setItem(row, 4, c_item)
        finally:
            self.suggestion_table.setUpdatesEnabled(True)

        self.suggestion_table.resizeRowsToContents()

        self.save_btn.setEnabled(True)
        self.status_label.setText(
            f"已生成执行建议：{self.current_suggestion['strategy_name']}（共推荐 {len(orders)} 项任务）"
        )
        self.tabs.setCurrentIndex(1)

    def save_suggestion_action(self, *args):
        if not self.current_suggestion:
            return
        try:
            self.store.save_suggestion(self.current_suggestion)
            self.save_btn.setEnabled(False)
            self.refresh_saved_list()
            self.refresh_tasks()
            QtWidgets.QMessageBox.information(
                self,
                "保存成功",
                "当前执行建议已成功保存！可在【已保存的建议】标签页中随时回顾。",
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self, "保存失败", f"保存执行建议出错：{e}"
            )

    def _on_saved_item_selected(self, current, _previous):
        if not current:
            self.saved_browser.clear()
            return
        s_id = current.data(QtCore.Qt.UserRole)
        for s in self.store.get_saved_suggestions():
            if s.get("id") == s_id:
                html = render_suggestion_html(s)
                self.saved_browser.setHtml(html)
                return

    def delete_saved_suggestion_action(self, *args):
        item = self.saved_list.currentItem()
        if not item:
            QtWidgets.QMessageBox.information(
                self, "提示", "请先选中要删除的历史建议。"
            )
            return
        s_id = item.data(QtCore.Qt.UserRole)
        btn = QtWidgets.QMessageBox.question(
            self,
            "确认删除",
            "确定要删除这条已保存的历史执行建议吗？",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
        )
        if btn == QtWidgets.QMessageBox.Yes:
            try:
                self.store.delete_suggestion(s_id)
                self.refresh_saved_list()
                self.refresh_tasks()
            except Exception as e:
                QtWidgets.QMessageBox.critical(
                    self, "删除失败", f"删除已保存建议出错：{e}"
                )


class DailyTasksPlugin:
    """Built-in plugin for Daily Task Management."""

    plugin_id = "daily_tasks"
    display_name = "每日任务管理"
    version = "1.0"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.store = None
        self.dialog = None

    def register(self, context):
        self.context = context
        context.register_command(
            PluginCommand(
                command_id="open",
                title="每日任务管理",
                callback=lambda _rows=None: self.open_manager(),
                locations=frozenset({MAIN_MENU}),
                tooltip="管理每日任务并生成兼具科学与幽默的智能执行建议",
                order=45,
            )
        )

    def start(self):
        if self.store is None:
            self.store = DailyTaskStore()

    def open_manager(self):
        if self.store is None:
            self.store = DailyTaskStore()
        if self.dialog is not None:
            try:
                # Probe if underlying C++ widget is alive
                _ = self.dialog.windowTitle()
            except RuntimeError:
                self.dialog = None

        if self.dialog is None:
            parent_widget = (
                self.context.parent_widget if self.context else None
            )
            self.dialog = DailyTasksDialog(self.store, parent=parent_widget)
            self.dialog.setModal(False)
            self.dialog.setWindowModality(QtCore.Qt.NonModal)
            if parent_widget is not None:
                try:
                    self.dialog.setWindowIcon(parent_widget.windowIcon())
                except Exception:
                    pass
            self.dialog.destroyed.connect(self._on_dialog_destroyed)
        else:
            self.dialog.refresh_all()

        if self.dialog.isMinimized():
            self.dialog.showNormal()
        else:
            self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog

    def _on_dialog_destroyed(self, *args):
        self.dialog = None

    def can_close(self):
        return True, ""

    def stop(self):
        if self.dialog is not None:
            try:
                self.dialog.close()
            except RuntimeError:
                pass
            self.dialog = None
