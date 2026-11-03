"""機器人的自我成長。

一級（自動）：每天 REFLECT_AT 回看昨天
  - 按大家的反應微調自己的幾個參數（語音頻率、主動找人頻率、說說草稿頻率），寫進 tunables.json，各插件用的時候讀
  - 從大家對機器人的反應裡記幾條相處經驗（觀察，不是規矩），之後聊天時帶上。
    總結時一併看現在的人設和主人定的規矩（設定 owner_rules），經驗不能和它們衝突；
    人設或規矩改了，下一分鐘就按最新的把舊經驗重整一遍（不等第二天）
二級（自動）：例行動作，只能用白名單裡的現有能力（群裡定時說句話 / 寫說說草稿 / 私信主人）
  可以讓它先看今天的聊天再說（群裡說話只看那個群，私信主人看和主人聊的，說說只看群聊）；今天沒聊天就不說
三級（主人審）：平時記下「機器人做不到但有人想要」的願望，每週五 PROPOSE_AT 整理成提案，配一張圖私信主人；
  用現有能力就能做到的直接做成例行動作，做不到的寫成給 Claude 等 agent 的需求書，主人拿去實現。
  私信主人時插件不寫固定句（10-26 用戶定）：開場她寫（人設＋這週的提案），提案清單和需求書是【后台】卡片；
  主人怎麼回都交給她，她用工具 lively_proposal 記下好／不要／做好了（只有主人說了才做得成），「提案 N 好」不再攔成指令
  按關鍵詞抓到的願望會再讓模型確認一次，測試、玩笑、本來就會的、故意定的規矩都篩掉
  能力清單按現在載入的插件現生成，關著的會標出來；故意定的規矩可以在設定 limits 裡補
主人私信的後台指令（回覆是【后台】白話）：成長 狀態/開/關/反思/經驗/刪經驗 N/重整經驗/重寫經驗/鎖 X/解鎖 X/還原/學會 描述，
  例行 列表/加 描述/刪 N/停 N/開 N/試 N/看今天 N/不看今天 N，提案 現在/列表，願望，願望 篩掉，本領。
  面板回提案照舊走「提案 N 好/不要 [做法]」（_owner_cmd），主人私信打這句交給她
"""
import asyncio
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import MessageChain, ResultContentType

from .lively_common import Settings, T2S, pick_final, tell_owner
from .review import Review
from .toolbox import Toolbox

TZ = timezone(timedelta(hours=8))
REFLECT_AT = "04:30"
PROPOSE_DAY, PROPOSE_AT = 4, "20:00"  # 週五 20:00，錯開 22:00 群漫畫和 22:30 日報
TUNE_COOLDOWN = 3 * 86400  # 同一個參數至少隔三天才再動
MAX_NOTES = 6
MAX_ROUTINES = 6
WISH_DAILY_MAX = 30
ROUTINE_HOURS = ("08:00", "22:30")
TODAY_LINES = 60  # 例行動作看今天的聊天時最多帶幾行
TODAY_CHARS = 3000

TUNABLES = {
    # key: (名字, 預設, 下限, 上限, 步長)
    "voice.p_dm": ("语音私聊概率", 0.12, 0.04, 0.25, 0.02),
    "voice.p_group": ("语音群聊概率", 0.05, 0.01, 0.10, 0.01),
    "proactive.chance": ("主动找人概率", 0.2, 0.08, 0.35, 0.04),
    "proactive.daily_max": ("主动找人每天上限", 2, 1, 4, 1),
    "qzone.draft_chance": ("说说草稿概率", 0.5, 0.2, 0.8, 0.1),
}
ALIASES = {"语音私聊": "voice.p_dm", "语音群聊": "voice.p_group", "主动找人": "proactive.chance",
           "主动上限": "proactive.daily_max", "说说": "qzone.draft_chance"}

VOICE_GOOD = re.compile(r"好听|可爱|好萌|萌死|再来一[条句个]|再发|多发|声音真|好甜|喜欢.{0,4}声音|声音.{0,4}喜欢")
VOICE_BAD = re.compile(r"别发语音|不要语音|不要发语音|听不了|不方便听|打字吧|发文字|难听|吵死|别说话|关掉语音|不想听")
CANT_RE = re.compile(r"做不到|办不到|没办法|不会[做弄搞查]|还不会|学不会|没有这个功能|没这功能|暂时还?不能|帮不了|查不了|看不到|听不到|做不了")
WISH_RE = re.compile(r"要是你会|要是你能|希望你能|你能不能学|学会|加个功能|加一个功能|新功能|能不能加|以后能不能")
GROWTH_ASK_RE = re.compile(r"成长|学会|新功能|新本领|新技能|进化|升级|变了|你会什么|你能做什么|会些什么|迭代|愿望|提案")

ROUTINE_CAPS = """- 例行动作（可以直接安排，不用写代码）：
  group_say：每天/每周某几天的固定时间在某个群说一句话（内容按提示现写）
  qzone_draft：固定时间写一条说说草稿（照常给〈主人〉审）
  owner_dm：固定时间私信〈主人〉一句话（提醒、问候）
  today：说之前先看今天的聊天（群里说话只看那个群，私信〈主人〉看和〈主人〉聊的，说说只看群聊）"""

WISH_CHECK_RULE = """有人跟〈我〉说了一句话，被当成了「希望〈我〉学会新本领」的愿望。请判断是不是真的。
〈我〉现在会的本领和故意定的规矩在下面。以下都不算愿望：
- 测试、闲聊、玩笑、恶作剧、随口抱怨
- 〈我〉本来就会的（只是这次没做好不算新本领）
- 故意定的规矩（比如画笔只给〈主人〉用，别人要画被拒绝）
- 针对某个人的、色情暴力的、要〈我〉越权的
只输出 JSON：{"real": true 或 false, "what": "想学的本领，一句话（不算就写空字符串）", "reason": "一句话理由"}"""

# 經驗只是她從大家反應裡看到的事，給她自己判斷時參考；人設和主人定的規矩才是準的（10-26 起）。
# 以前的規則讓它「不能讓〈我〉罵人」、「主人的糾正最重要」，又看不到人設，結果把主人一次試功能、
# 群友起哄都寫成了「別怼」「短句擋回」「保持害羞」這種全局命令，和人設打架
NOTES_BASIS = """〈我〉现在的人设（最新的，以这个为准）：
{persona}

〈主人〉定的规矩（最新的，以这个为准）：
{rules}"""

NOTE_RULES = """- 人设和〈主人〉定的规矩是最新的：经验不能和它们冲突，旧经验和它们冲突的直接删掉；人设里已经写了的不用重复
- 尺度不归经验管：骂不骂人、怼到什么程度、亲密到哪一步、发不发印象卡、接不接起哄，都由人设、这一轮的分寸和〈我〉当场的判断决定，经验里不写这些
- 写成看到的事，不写命令：例如「X 被怼了还接着玩，不像真生气」「Y 深夜来私聊多半是想有人陪着说话」，不要写「别怼」「先照做」「短句带过」
- 〈主人〉在聊天里临时让〈我〉怎么说、演一段、试功能、试边界，是那一次的事，不写成经验
- 群友的玩笑、起哄先当玩笑看；不要把一个人的话算到别人（尤其〈主人〉）头上"""

REFLECT_RULE = """你是〈名字〉在睡前复盘。下面有〈我〉现在的人设和〈主人〉定的规矩、〈我〉以前记下的相处经验（可能已经过时）、昨天的对话片段（〈我〉说的话、前面一句、之后三分钟内别人的话）。
请重写「相处经验」：最多 6 条，每条一句话，记的是大家的真实反应——谁喜欢什么样的互动、谁对什么话题冷场或不高兴、什么场合大家爱接话——给〈我〉以后自己判断时参考。
规则：
""" + NOTE_RULES + """
- 只从对话里看得到的反应总结，每条都要写依据（哪段对话里谁怎么反应）；看不出明显反应就不写，宁少勿编；旧经验昨天没再出现、也不冲突的可以留着
只输出 JSON：{"notes": [{"text": "经验，一句话", "from": "依据，一句话"}], "changed": "一句话说这次改了什么，没改就写空字符串"}"""

ALIGN_RULE = """〈我〉的人设或〈主人〉定的规矩刚改过。下面是新的人设和规矩，以及〈我〉以前记下的相处经验。
请按新的人设和规矩把相处经验重整一遍：
""" + NOTE_RULES + """
- 不冲突的原样留着（依据照抄），不要新编经验
只输出 JSON：{"notes": [{"text": "经验，一句话", "from": "依据，一句话"}], "dropped": [{"text": "删掉或改掉的那条", "why": "一句话"}], "changed": "一句话说改了什么，没改就写空字符串"}"""

PROPOSE_RULE = """你是〈名字〉，在整理这一周大家希望〈我〉学会的新本领，周五交给主人（〈主人〉）审阅。
输入：本周的愿望记录（什么时候、谁、在哪、说了什么、〈我〉当时怎么回的）、〈我〉现在会的本领、〈我〉手上现有的工具、已有的例行动作、以前提过的提案。
要做的：
1. 合并同类愿望，扔掉玩笑话、恶作剧、针对某个人的、色情暴力的、要〈我〉越权的（比如给别人画画、替别人发群公告）
2. 用现有「例行动作」就能满足的，写进 routines（最多 1 个），不用写提案
3. 现有工具已经能做到的（愿望可能是工具做出来以前记的），不写提案，写进 covered：哪条愿望、哪个工具、现在怎么用；只是部分做到的，提案只写还缺的那块
4. 需要写新代码才能做到的，写进 proposals（最多 3 个，按价值排序）；以前提过的不重复提
5. brief 是给 Claude 这类编程 agent 看的需求书：〈名字〉跑在 AstrBot v4 + NapCat(QQ) + DeepSeek 上，功能用 AstrBot 插件实现；
   写清楚要做什么、谁会用、怎么触发、要用到什么外部服务或数据、需要的权限、怎么算做好了，不要写代码
只输出 JSON：
{"routines": [{"name": "…", "action": "group_say|qzone_draft|owner_dm", "gid": "群号（group_say 才要）", "prompt": "每次现写时的提示", "today": "内容要用到今天聊过的事就写 true，否则 false", "days": [0-6，周一是0，每天就写全部], "at": "HH:MM", "why": "…"}],
 "covered": [{"wish": "愿望，一句话", "tool": "工具名", "how": "现在怎么用，一句话"}],
 "proposals": [{"title": "…", "who": "谁想要（名字，几个人）", "evidence": ["原话1", "原话2"], "idea": "〈我〉想怎么做，一两句", "needs": "要用到什么", "risk": "风险", "effort": "小|中|大", "brief": "需求书"}],
 "cover": "封面插画的英文描述：只写动作、场景和道具，不要描述〈名字〉的长相（画的时候会另外给），例如她抱着写满愿望的长卷轴向〈主人〉汇报，旁边放一两个和本周提案有关的小道具"}
没有值得提的就让 proposals 为空数组"""

ROUTINE_PARSE_RULE = """把主人的话解析成一个例行动作，只输出 JSON：
{"name": "…", "action": "group_say|qzone_draft|owner_dm", "gid": "群号（group_say 才要，从下面可用的群里选）", "prompt": "每次现写内容时的提示", "today": "内容要用到今天聊过的事就写 true，否则 false", "days": [0-6，周一是0], "at": "HH:MM"}
可用的群：{groups}。做不到就输出 {"error": "原因"}"""

SAY_RULE = """现在是 {now}，〈我〉按自己的例行安排说一句话：{prompt}
{context}像平时聊天那样一两句就好，不要说这是例行安排，不要 @ 任何人，不点名针对任何人。
只输出要说的话本身，不要先写分析、情景、清单或「写：」这类草稿"""

# 「提案 N 好 [做法]」：面板回提案用的格式（主人私信打這句不攔，交給她）
DECIDE_RE = re.compile(r"^#?(\d+)\s*(好|可以|同意|行|不要|不行|算了|做好了|完成了|学会了)\s*(.*)$", re.S)
TODAY_HINT = "下面是今天的聊天记录，只能说这里面真实发生过的事，不要编：\n{lines}\n\n"


def system_tail(req, text: str):
    """每輪都一樣的說明接在 system 最後：排在對話歷史前面，DeepSeek 前綴快取命中得到（已經有了就不重複加）"""
    sp = getattr(req, "system_prompt", "") or ""
    if text and text not in sp:
        req.system_prompt = f"{sp.rstrip()}\n\n{text}" if sp.strip() else text


def _ro(path: Path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _true(v) -> bool:
    """模型輸出的 true 可能是字串"""
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


def _fmt(key: str, v) -> str:
    return f"{v:g}" if TUNABLES[key][1] >= 1 else f"{round(v * 100)}%"



def _cn_name(st) -> str:
    """插件的中文名（metadata.yaml 的 cn_name；10-31 起 display_name 是拉丁名，給〈我〉看的清單用中文的）"""
    try:
        meta = Path(__file__).resolve().parent.parent / str(st.root_dir_name or "") / "metadata.yaml"
        m = re.search(r"^cn_name:\s*([^#\n]+)", meta.read_text("utf-8"), re.M)
        return m.group(1).strip() if m else ""
    except Exception:
        return ""

@register(
    "astrbot_plugin_lively_evolve",
    "GUCine",
    "自我成長：按大家的反應微調參數、總結相處經驗、例行動作、每週五把群友願望整理成提案私信主人",
    "2.1.0",
)
class LivelyEvolve(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.s = Settings(context, config)
        self.dir = Path(StarTools.get_data_dir("astrbot_plugin_lively_evolve"))
        self.data = self.dir.parent
        self.root = self.data.parent
        self.state_file = self.dir / "state.json"
        self.tune_file = self.dir / "tunables.json"
        self.wish_file = self.dir / "wishes.jsonl"
        self.events_file = self.dir / "events.jsonl"
        self.state = self._load()
        self.tunes = self._load_tunes()
        self._busy = asyncio.Lock()
        self._wish_day, self._wish_n, self._wish_seen = "", 0, set()
        self.review = Review(self, self.dir / "review")  # 重要改動先問 Claude 姐姐（10-30）
        self.toolbox = Toolbox(self, self.dir / "toolbox.json")
        self._task = asyncio.create_task(self._loop())

    async def terminate(self):
        self._task.cancel()

    # ---------- 狀態 ----------
    def _load(self) -> dict:
        try:
            s = json.loads(self.state_file.read_text("utf-8"))
        except Exception:
            s = {}
        s.setdefault("enabled", True)
        s.setdefault("notes", [])  # [{"text", "ts"}]
        s.setdefault("routines", [])  # [{"id", "name", "action", "gid", "prompt", "today", "days", "at", "by", "enabled", "last_day", "runs", "ts"}]
        for r in s["routines"]:
            # 2.1.0 以前沒有 today：提示裡寫了「今天」的就是要看今天的聊天
            r.setdefault("today", bool(re.search(r"今天|今日", str(r.get("prompt") or ""))))
        s.setdefault("next_rid", 1)
        s.setdefault("last_reflect_day", "")
        s.setdefault("last_propose_week", "")
        s.setdefault("propose_tries", {})
        s.setdefault("proposals", [])  # [{"n", "week", "title", ..., "status": pending|ok|no|done}]
        s.setdefault("learned", [])  # [{"text", "ts"}]
        s.setdefault("history", [])  # 參數改動 [{"key", "old", "new", "why", "ts"}]
        return s

    def _save(self):
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self.state_file)
        except Exception as e:
            logger.error(f"[lively_evolve] 保存状态失败: {e}")

    def _load_tunes(self) -> dict:
        try:
            t = json.loads(self.tune_file.read_text("utf-8"))
        except Exception:
            t = {}
        t.setdefault("values", {})
        t.setdefault("locked", [])
        t.setdefault("changed", {})  # key -> ts
        for k, spec in TUNABLES.items():
            t["values"].setdefault(k, spec[1])
        return t

    def _save_tunes(self):
        try:
            tmp = self.tune_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.tunes, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self.tune_file)
        except Exception as e:
            logger.error(f"[lively_evolve] 保存参数失败: {e}")

    def _log(self, kind: str, **data):
        data = {k: self.s.fill(v) if isinstance(v, str) else v for k, v in data.items()}
        try:
            with self.events_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()), "kind": kind, **data}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    @staticmethod
    def _jsonl(path: Path, since: float) -> list[dict]:
        out = []
        if not path.exists():
            return out
        for line in path.read_text("utf-8").splitlines():
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("ts", 0) >= since:
                out.append(e)
        return out

    def _persona(self) -> str:
        return self.s.persona_prompt()

    def _groups(self) -> dict[str, str]:
        names = {g: f"群{g}" for g in self.s.groups()}
        try:
            con = _ro(self.root / "data_v4.db")
            for umo, name in con.execute("select umo, auto_name from umo_aliases"):
                gid = umo.rsplit(":", 1)[-1]
                if ":GroupMessage:" in umo and gid in names and name:
                    names[gid] = name
            con.close()
        except Exception:
            pass
        return names

    async def _llm(self, prompt: str, system: str, timeout: int = 90) -> str:
        prov = self.s.provider()
        if prov is None:
            raise RuntimeError("没有可用的模型")
        resp = await asyncio.wait_for(prov.text_chat(prompt=self.s.fill(prompt), system_prompt=self.s.fill(system)), timeout=timeout)
        return (getattr(resp, "completion_text", "") or "").strip()

    async def _tell(self, facts: str, what: str, must: bool = False, card=None, image: str | None = None) -> str:
        """告訴主人：她寫一句（人設＋事實），must 的再發【后台】卡片。插件不寫冒充她口吻的句子"""
        said = await tell_owner(self.s, facts, what, must=must, card=card or "", image=image, kind="成长")
        self._log("report", text=f"{what}：" + (said[:120] or "（没私信）"))
        return said

    # ---------- 定時 ----------
    async def _loop(self):
        await asyncio.sleep(90)
        while True:
            try:
                now = datetime.now(TZ)
                day, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
                if self.state["enabled"]:
                    if hm >= REFLECT_AT and self.state["last_reflect_day"] != day:
                        self.state["last_reflect_day"] = day
                        self._save()
                        await self.reflect(now)
                    week = now.strftime("%G-W%V")
                    tries = self.state["propose_tries"].get(week, 0)
                    if (now.weekday() == PROPOSE_DAY and hm >= PROPOSE_AT
                            and self.state["last_propose_week"] != week and tries < 2):
                        self.state["propose_tries"] = {week: tries + 1}
                        self._save()
                        await self.propose()
                    await self._run_routines(now)
                    if self.state["notes"] and self.state.get("notes_basis") != self._basis()[1] and not self._busy.locked():
                        async with self._busy:
                            await self.align_notes()
                # 審核單不看「成長」開關：她交了的單照樣要有人回
                await self.review.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[lively_evolve] 定时任务出错: {e}", exc_info=True)
            await asyncio.sleep(30)

    # ---------- 一級：調參數 ----------
    def _voice_signal(self, since: float) -> dict:
        """語音發出去之後 10 分鐘內同一個聊天裡的反應"""
        sent = [e for e in self._jsonl(self.data / "astrbot_plugin_lively_voice" / "events.jsonl", since) if e.get("kind") == "sent"]
        out = {"dm": [0, 0, 0], "group": [0, 0, 0]}  # 發了幾條, 好評, 差評
        if not sent:
            return out
        try:
            con = _ro(self.data / "astrbot_plugin_livingmemory" / "conversations.db")
            for e in sent:
                scope = "group" if ":GroupMessage:" in e.get("umo", "") else "dm"
                out[scope][0] += 1
                rows = con.execute(
                    "select content from messages where session_id = ? and role = 'user' "
                    "and timestamp > ? and timestamp < ? limit 30",
                    (e["umo"], e["ts"], e["ts"] + 600)).fetchall()
                text = " ".join(str(r[0]) for r in rows)
                out[scope][1] += bool(VOICE_GOOD.search(text))
                out[scope][2] += bool(VOICE_BAD.search(text))
            con.close()
        except Exception as e:
            logger.warning(f"[lively_evolve] 读语音反应失败: {e}")
        return out

    def _decide(self, now: float) -> list[tuple[str, int, str]]:
        """返回 [(key, 方向, 理由)]"""
        moves = []
        v = self._voice_signal(now - 3 * 86400)
        for scope, key in (("dm", "voice.p_dm"), ("group", "voice.p_group")):
            n, good, bad = v[scope]
            where = "私聊" if scope == "dm" else "群里"
            if bad >= 2 and bad > good:
                moves.append((key, -1, f"最近{where}发的 {n} 条语音里有 {bad} 次有人嫌"))
            elif good >= 3 and bad == 0:
                moves.append((key, 1, f"最近{where}发的 {n} 条语音被夸了 {good} 次"))

        evs = self._jsonl(self.data / "astrbot_plugin_lively_proactive" / "events.jsonl", now - 14 * 86400)
        sent = sum(1 for e in evs if e.get("kind") == "sent")
        replied = len({e.get("qq") for e in evs if e.get("kind") == "replied"})
        if sent >= 3:
            rate = replied / sent
            if rate >= 0.6:
                moves.append(("proactive.chance", 1, f"两周主动找了 {sent} 次人，{replied} 个回了"))
                if rate >= 0.75 and sent >= 5:
                    moves.append(("proactive.daily_max", 1, f"主动找人大家几乎都回（{replied}/{sent}）"))
            elif rate <= 0.25:
                moves.append(("proactive.chance", -1, f"两周主动找了 {sent} 次人，只有 {replied} 个回"))
                moves.append(("proactive.daily_max", -1, "主动找人回的人太少，先少打扰"))

        evs = self._jsonl(self.data / "astrbot_plugin_lively_qzone" / "events.jsonl", now - 14 * 86400)
        posted = sum(1 for e in evs if e.get("kind") == "posted")
        bad = sum(1 for e in evs if e.get("kind") in ("rejected", "deleted"))
        if bad >= 2 and bad > posted:
            moves.append(("qzone.draft_chance", -1, f"两周写的说说草稿被〈主人〉退了或删了 {bad} 次"))
        elif posted >= 3 and bad == 0:
            moves.append(("qzone.draft_chance", 1, f"两周发了 {posted} 条说说都没被退"))
        return moves

    def tune(self) -> list[str]:
        now = time.time()
        done = []
        for key, sign, why in self._decide(now):
            if key in self.tunes["locked"] or now - self.tunes["changed"].get(key, 0) < TUNE_COOLDOWN:
                continue
            name, _, lo, hi, step = TUNABLES[key]
            old = self.tunes["values"][key]
            new = round(min(hi, max(lo, old + sign * step)), 3)
            if new == old:
                continue
            self.tunes["values"][key] = new
            self.tunes["changed"][key] = int(now)
            self.state["history"] = (self.state["history"] + [
                {"key": key, "old": old, "new": new, "why": why, "ts": int(now)}])[-50:]
            line = f"{name} {_fmt(key, old)} → {_fmt(key, new)}（{why}）"
            self._log("tune", key=key, old=old, new=new, why=why, text=line)
            done.append(line)
        if done:
            self._save_tunes()
            self._save()
        return done

    # ---------- 一級：總結經驗 ----------
    def _exchanges(self, t0: float, t1: float, limit: int = 50) -> list[str]:
        con = _ro(self.data / "astrbot_plugin_livingmemory" / "conversations.db")
        mine = con.execute(
            "select session_id, content, timestamp from messages where role = 'assistant' "
            "and timestamp >= ? and timestamp < ? order by timestamp", (t0, t1)).fetchall()
        out = []
        for sid, content, ts in mine:
            if ":GroupMessage:" in sid and sid.rsplit(":", 1)[-1] not in self.s.groups():
                continue
            before = con.execute(
                "select sender_name, sender_id, content from messages where session_id = ? and role = 'user' "
                "and timestamp < ? order by timestamp desc limit 1", (sid, ts)).fetchone()
            after = con.execute(
                "select sender_name, sender_id, content from messages where session_id = ? and role = 'user' "
                "and timestamp > ? and timestamp < ? order by timestamp limit 2", (sid, ts, ts + 180)).fetchall()
            who = lambda r: ("〈主人〉" if self.s.is_owner(r[1]) else (r[0] or r[1]))
            where = "私聊" if ":FriendMessage:" in sid else "群聊"
            lines = [f"[{where}]"]
            if before:
                lines.append(f"{who(before)}：{str(before[2])[:80]}")
            lines.append(f"〈我〉：{str(content)[:100]}")
            lines += [f"→ {who(r)}：{str(r[2])[:80]}" for r in after]
            # 之後有人點名回它（喊名字或 @）加分；名字和 QQ 都不知道時只看有沒有人接話
            me = "|".join(x for x in (re.escape(self.s.bot_name()), f"At:{self.s.bot_qq()}" if self.s.bot_qq() else "") if x)
            score = 2 * bool(me and any(re.search(me, str(r[2])) for r in after)) + len(after)
            out.append((score, "\n".join(lines)))
        con.close()
        out.sort(key=lambda x: -x[0])
        return [x[1] for x in out[:limit]]

    def _owner_rules(self) -> list[str]:
        return [self.s.fill(str(x).strip()) for x in (self.s.get("owner_rules", []) or []) if str(x).strip()]

    def _basis(self) -> tuple[str, str]:
        """經驗要對齊的東西（人設＋主人定的規矩）和它的指紋；改了就要重整經驗"""
        rules = self._owner_rules()
        text = NOTES_BASIS.format(persona=self._persona() or "（读不到人设）",
                                  rules="\n".join(f"- {x}" for x in rules) or "（没有另外定的）")
        return text, hashlib.sha1(text.encode()).hexdigest()[:12]

    @staticmethod
    def _parse_notes(data: dict) -> list[dict]:
        out = []
        for x in data.get("notes") or []:
            text, frm = (x.get("text"), x.get("from")) if isinstance(x, dict) else (x, "")
            text = str(text or "").strip()[:80]
            if text:
                out.append({"text": text, "from": str(frm or "").strip()[:80]})
        return out[:MAX_NOTES]

    def _old_notes(self) -> str:
        return "\n".join(f"- {n['text']}" + (f"（依据：{n['from']}）" if n.get("from") else "") for n in self.state["notes"]) or "（还没有）"

    def _set_notes(self, notes: list[dict], basis: str, kind: str, changed: str, **extra) -> str:
        keep = {n["text"]: n["ts"] for n in self.state["notes"]}
        old = [n["text"] for n in self.state["notes"]]
        self.state["notes"] = [{**n, "ts": keep.get(n["text"], int(time.time()))} for n in notes]
        self.state["notes_basis"] = basis
        self._save()
        if [n["text"] for n in notes] == old:
            return ""
        changed = changed or "更新了相处经验"
        self._log(kind, text=changed, notes=[n["text"] for n in notes], **extra)
        return changed

    async def update_notes(self, now: datetime) -> str:
        start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        ex = self._exchanges(start.timestamp(), start.timestamp() + 86400)
        if len(ex) < 5:
            return await self.align_notes()
        basis_text, basis = self._basis()
        prompt = (basis_text + "\n\n以前记下的相处经验（可能已经过时）：\n" + self._old_notes()
                  + "\n\n昨天的对话片段：\n\n" + "\n\n".join(ex))
        data = _json(await self._llm(prompt, REFLECT_RULE, timeout=180))
        notes = self._parse_notes(data)
        if not notes and self.state["notes"]:
            return ""  # 模型沒給出能用的：舊的先留著，別清空
        return self._set_notes(notes, basis, "notes", str(data.get("changed") or "").strip()[:120])

    async def align_notes(self, force: bool = False) -> str:
        """人設或主人定的規矩改了：按最新的把舊經驗重整一遍（只刪、只改，不新編）"""
        basis_text, basis = self._basis()
        if not self.state["notes"]:
            self.state["notes_basis"] = basis
            self._save()
            return ""
        if not force and self.state.get("notes_basis") == basis:
            return ""
        prompt = basis_text + "\n\n以前记下的相处经验：\n" + self._old_notes()
        data = _json(await self._llm(prompt, ALIGN_RULE, timeout=180))
        if "notes" not in data:
            raise RuntimeError("模型没给出能解析的经验")
        dropped = [{"text": str(d.get("text") or "")[:80], "why": str(d.get("why") or "")[:80]}
                   for d in (data.get("dropped") or []) if isinstance(d, dict)]
        changed = str(data.get("changed") or "").strip()[:120] or "按最新的人设和规矩重整了相处经验"
        out = self._set_notes(self._parse_notes(data), basis, "notes_align", changed, dropped=dropped)
        logger.info(f"[lively_evolve] 人设或规矩改了，重整相处经验：{out or '不用改'}"
                    + (f"；删改 {len(dropped)} 条" if dropped else ""))
        return out

    async def reflect(self, now: datetime) -> str:
        async with self._busy:
            lines = self.tune()
            try:
                changed = await self.update_notes(now)
            except Exception as e:
                logger.warning(f"[lively_evolve] 总结经验失败: {e}")
                changed = ""
            if changed:
                lines.append("经验：" + changed)
            logger.info(f"[lively_evolve] 每日复盘完成：{lines or '没有变化'}")
            return "\n".join(lines) or "昨天没什么需要调整的"

    # ---------- 聊天時帶上經驗和成長記錄 ----------
    @filter.on_llm_request(priority=2)
    async def inject(self, event: AstrMessageEvent, req):
        if not self.state["enabled"]:
            return
        parts = []
        if self.state["notes"]:
            notes = ("[系统提示，不要复述] 〈我〉从大家的反应里记下的事（只是参考，和人设、这一轮的分寸不一样时以它们为准）：\n"
                     + "\n".join(f"- {n['text']}" for n in self.state["notes"]))
            # 相處經驗一天才變一次：放 system 最後，命中得到快取；關掉 rules_in_system 就照舊掛在這輪消息後面
            if self.s.shared("rules_in_system", True):
                system_tail(req, self.s.fill(notes))
            else:
                parts.append(notes)
        if box := self.toolbox.names_line():
            parts.append(box)
        if GROWTH_ASK_RE.search((event.message_str or "").translate(T2S)):
            g = ["[系统提示，不要复述] 〈我〉的成长：〈我〉会根据大家的反应自己调整，每周五把大家希望〈我〉学的新本领整理成提案交给〈主人〉"]
            g += [f"- {datetime.fromtimestamp(x['ts'], TZ):%m-%d} 学会了：{x['text']}" for x in self.state["learned"][-5:]]
            g += [f"- {datetime.fromtimestamp(h['ts'], TZ):%m-%d} 调整了{TUNABLES[h['key']][0]}：{h['why']}"
                  for h in self.state["history"][-3:] if h["key"] in TUNABLES]
            pend = [p["title"] + (f"（〈主人〉定的做法：{p['note'][:80]}）" if p.get("note") else "")
                    for p in self.state["proposals"] if p.get("status") in ("pending", "ok")]
            if pend:
                g.append("- 已经提给〈主人〉、还在等的：" + "、".join(pend[-5:]))
            parts.append("\n".join(g))
        for p in parts:
            try:
                req.extra_user_content_parts.append(TextPart(text=self.s.fill(p)).mark_as_temp())
            except Exception:
                pass

    # ---------- 三級：記願望 ----------
    def _add_wish(self, event: AstrMessageEvent, src: str, ask: str, reply: str = "", why: str = "") -> bool:
        """模型自己調工具記的直接寫；按關鍵詞抓到的先在背景讓模型確認一次，確認完再寫（篩掉的也寫，標 check=no）"""
        day = datetime.now(TZ).strftime("%Y-%m-%d")
        if self._wish_day != day:
            self._wish_day, self._wish_n, self._wish_seen = day, 0, set()
        if self._wish_n >= WISH_DAILY_MAX or (src != "tool" and ask in self._wish_seen):
            return False
        self._wish_n += 1
        self._wish_seen.add(ask)
        w = {"ts": int(time.time()), "src": src, "qq": str(event.get_sender_id() or ""),
             "name": event.get_sender_name() or "", "gid": str(event.get_group_id() or ""),
             "ask": ask[:200], "reply": reply[:150], "why": why[:150]}
        if src == "tool":
            return self._write_wish(w)
        asyncio.create_task(self._check_wish(w))
        return True

    def _write_wish(self, w: dict) -> bool:
        try:
            with self.wish_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(w, ensure_ascii=False) + "\n")
        except Exception:
            return False
        return True

    async def _check_wish(self, w: dict):
        who = "〈主人〉" if self.s.is_owner(w["qq"]) else (w["name"] or "群友")
        prompt = (self._capabilities() + f"\n\n{who}说：{w['ask']}\n〈我〉当时回：{w['reply'] or '（没回）'}")
        try:
            data = _json(await self._llm(prompt, WISH_CHECK_RULE, timeout=60))
        except Exception as e:
            logger.warning(f"[lively_evolve] 确认愿望失败，先留着: {e}")
            data = {}
        if "real" not in data:
            w["check"] = "?"  # 沒確認成，留給週五整理提案時再篩
        else:
            w["check"] = "yes" if _true(data.get("real")) else "no"
            w["what"] = str(data.get("what") or "").strip()[:100]
            w["check_why"] = str(data.get("reason") or "").strip()[:100]
        self._write_wish(w)
        logger.info(f"[lively_evolve] 愿望{'筛掉' if w['check'] == 'no' else '记下'}：{w['ask'][:40]}"
                    f"（{w.get('check_why') or '没确认成'}）")

    @filter.on_decorating_result(priority=50)
    async def watch_reply(self, event: AstrMessageEvent):
        """機器人說了「做不到」，或者有人直接說希望機器人會什麼，記下來"""
        result = event.get_result()
        if getattr(result, "result_content_type", None) != ResultContentType.LLM_RESULT:
            return
        chain = getattr(result, "chain", None) or []
        reply = "".join(c.text for c in chain if isinstance(c, Plain)).strip()
        ask = (event.message_str or "").strip()
        if not ask or event.get_extra("lively_wish_logged") or event.get_extra("lively_beat"):  # 〈我〉看手機時想說話的內部消息：不是誰的願望
            return
        reply_s, ask_s, me, name = reply.translate(T2S), ask.translate(T2S), self.s.me(), self.s.bot_name()
        if CANT_RE.search(reply_s) or f"{me}不能" in reply_s:
            self._add_wish(event, "cant", ask, reply)
        elif WISH_RE.search(ask_s) or (name and (f"希望{name}" in ask_s or f"{name}能不能学" in ask_s)):
            self._add_wish(event, "wish", ask, reply)

    @filter.llm_tool("lively_wish")
    async def wish_tool(self, event: AstrMessageEvent, what: str, why: str = ""):
        """把一样你现在做不到的本领记进你的愿望清单（有人希望你会、或者你自己发现缺了，你也想学的时候可以用）。每周五你会把清单整理成提案交给主人，主人同意了才会做；不一定每样都会做。

        Args:
            what(string): 想学的本领，一句话
            why(string): 谁想要、为什么，一句话
        """
        event.set_extra("lively_wish_logged", True)
        ok = self._add_wish(event, "tool", f"{what}（原话：{(event.message_str or '')[:120]}）", why=why)
        return "记下了" if ok else "今天的愿望清单记满了"

    # ---------- 二級：例行動作 ----------
    def _valid_routine(self, r: dict) -> str:
        if r.get("action") not in ("group_say", "qzone_draft", "owner_dm"):
            return "动作不在白名单里"
        if r["action"] == "group_say" and str(r.get("gid")) not in self.s.groups():
            return self.s.fill("只能在〈我〉服务的群里说话")
        if not re.fullmatch(r"\d\d:\d\d", str(r.get("at", ""))):
            return "时间不对"
        if not (ROUTINE_HOURS[0] <= r["at"] <= ROUTINE_HOURS[1]):
            return f"只能安排在 {ROUTINE_HOURS[0]}～{ROUTINE_HOURS[1]}"
        days = r.get("days")
        if not isinstance(days, list) or not days or not all(isinstance(d, int) and 0 <= d <= 6 for d in days):
            return "日子不对"
        prompt = str(r.get("prompt") or "")
        if not prompt or len(prompt) > 200:
            return "内容提示不对"
        if re.search(r"\d{5,}|At:", prompt):
            return "内容提示里不能有 QQ 号"
        if len([x for x in self.state["routines"] if x.get("enabled")]) >= MAX_ROUTINES:
            return f"例行动作最多 {MAX_ROUTINES} 个"
        return ""

    def _add_routine(self, r: dict, by: str) -> tuple[dict | None, str]:
        r = {k: r.get(k) for k in ("name", "action", "gid", "prompt", "today", "days", "at")}
        r["days"] = sorted({int(d) for d in (r.get("days") or []) if str(d).isdigit()})
        r["gid"] = str(r.get("gid") or "")
        r["today"] = _true(r.get("today"))
        err = self._valid_routine(r)
        if err:
            return None, err
        r.update(id=self.state["next_rid"], name=str(r.get("name") or "例行")[:20], by=by, enabled=True,
                 last_day="", runs=0, ts=int(time.time()))
        self.state["next_rid"] += 1
        self.state["routines"].append(r)
        self._save()
        self._log("routine", text=f"新例行：{self._desc(r)}", by=by)
        return r, ""

    def _desc(self, r: dict) -> str:
        names = "一二三四五六日"
        days = "每天" if len(r["days"]) == 7 else "每周" + "、".join(names[d] for d in r["days"])
        where = {"group_say": f"在{self._groups().get(r.get('gid'), r.get('gid'))}说话",
                 "qzone_draft": "写说说草稿", "owner_dm": "私信〈主人〉"}[r["action"]]
        return (f"#{r['id']} {r['name']}：{days} {r['at']} {where}{'，先看今天的聊天' if r.get('today') else ''}"
                f"（{r['prompt'][:40]}）{'' if r.get('enabled') else '【停了】'}")

    async def _run_routines(self, now: datetime):
        day, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        for r in self.state["routines"]:
            if not r.get("enabled") or r.get("last_day") == day or now.weekday() not in r["days"]:
                continue
            # 過點 30 分鐘內才補跑，重啟錯過太久就算了
            due = now.replace(hour=int(r["at"][:2]), minute=int(r["at"][3:]), second=0, microsecond=0)
            if not (r["at"] <= hm and now - due < timedelta(minutes=30)):
                continue
            r["last_day"] = day
            self._save()
            try:
                msg = await self.run_routine(r)
                if not msg:
                    logger.info(f"[lively_evolve] 例行 #{r['id']} {r['name']}：今天没有能用的聊天，这次不说")
                    self._log("routine_skip", id=r["id"], text="今天没有能用的聊天")
                    continue
                r["runs"] = r.get("runs", 0) + 1
                self._save()
                self._log("routine_run", id=r["id"], text=msg[:100])
            except Exception as e:
                logger.warning(f"[lively_evolve] 例行 #{r['id']} 失败: {e}")

    def _today_chat(self, r: dict) -> list[str]:
        """例行動作要看的今天的聊天。群裡說話只看那個群；私信主人看和主人的私聊，加上主人在群裡跟〈我〉說的；
        說說是公開的，只看群聊，私聊的事不能寫進去"""
        start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        cols = "session_id, role, sender_id, sender_name, content, timestamp"
        con = _ro(self.data / "astrbot_plugin_livingmemory" / "conversations.db")
        try:
            if r["action"] == "group_say":
                rows = con.execute(
                    f"select {cols} from messages where session_id like ? and timestamp >= ? "
                    "order by timestamp desc limit ?", (f"%:GroupMessage:{r['gid']}", start, TODAY_LINES)).fetchall()
            elif r["action"] == "owner_dm":
                owner = self.s.owner()
                rows = con.execute(
                    f"select {cols} from messages where session_id like ? and timestamp >= ? "
                    "order by timestamp desc limit ?", (f"%:FriendMessage:{owner}", start, TODAY_LINES)).fetchall()
                # 主人在群裡點名〈我〉說的話（@ 或喊名字），不是主人在群裡說的每一句
                cond, args = self.s.talked_to_bot()
                said = con.execute(
                    f"select {cols} from messages where session_id like '%:GroupMessage:%' and role = 'user' "
                    f"and sender_id = ? and timestamp >= ? and {cond} order by timestamp desc limit ?",
                    (owner, start, *args, TODAY_LINES // 3)).fetchall()
                for row in said:
                    if row[0].rsplit(":", 1)[-1] not in self.s.groups():
                        continue
                    rows.append(row)
                    # 〈我〉接著回主人的那句
                    rows += con.execute(
                        f"select {cols} from messages where session_id = ? and role = 'assistant' "
                        "and timestamp > ? and timestamp < ? order by timestamp limit 1",
                        (row[0], row[5], row[5] + 90)).fetchall()
            else:
                gids = self.s.groups()
                rows = [x for x in con.execute(
                    f"select {cols} from messages where session_id like '%:GroupMessage:%' and timestamp >= ? "
                    "order by timestamp desc limit ?", (start, TODAY_LINES * 3)).fetchall()
                        if x[0].rsplit(":", 1)[-1] in gids][:TODAY_LINES]
        finally:
            con.close()
        groups, bot = self._groups(), self.s.bot_qq()
        out, seen = [], set()
        for sid, role, qq, name, content, ts in sorted(rows, key=lambda x: x[5]):
            text = re.sub(r"\s+", " ", str(content or "")).strip()
            if bot:
                text = text.replace(f"[At:{bot}]", "@〈我〉")
            text = re.sub(r"\[At:\d+\]", "@某人", text)
            # 只有 [图片]、[转发消息] 這類的行沒內容，不帶
            if not text or re.fullmatch(r"(?:\[[^\]]{1,8}\]\s*)+", text) or (sid, ts, role) in seen:
                continue
            seen.add((sid, ts, role))
            who = "〈我〉" if role == "assistant" else ("〈主人〉" if self.s.is_owner(qq) else (name or "群友"))
            where = "" if r["action"] == "group_say" else (
                "私聊 " if ":FriendMessage:" in sid else f"[{groups.get(sid.rsplit(':', 1)[-1], '群')[:8]}] ")
            out.append(f"{datetime.fromtimestamp(ts, TZ):%H:%M} {where}{who}：{text[:80]}")
        # 太長從前面砍，留最近的
        while out and sum(len(x) + 1 for x in out) > TODAY_CHARS:
            out.pop(0)
        return out

    async def run_routine(self, r: dict) -> str:
        """返回說出去的話；要看今天的聊天但今天沒有就返回空字串，這次不說"""
        context = ""
        if r.get("today"):
            lines = self._today_chat(r)
            if not lines:
                return ""
            context = TODAY_HINT.format(lines="\n".join(lines))
        if r["action"] == "qzone_draft":
            star = self.context.get_registered_star("astrbot_plugin_lively_qzone")
            if not star or not star.star_cls:
                raise RuntimeError("说说插件没加载")
            topic = r["prompt"] + (("\n\n" + context + "说说是公开的，不要点名、不要写谁的私事") if context else "")
            return await star.star_cls.new_draft(self.s.fill(topic))
        n = datetime.now(TZ)
        now = f"{n:%m月%d日} 周{'一二三四五六日'[n.weekday()]} {n:%H:%M}"
        text = await self._llm(SAY_RULE.format(now=now, prompt=r["prompt"], context=context), self._persona())
        text = re.sub(r"(?:<<|《《|＜＜)[^\n]*", "", text)
        final = pick_final(text)  # 把起草稿寫成正文了：挑出終稿，挑不出來這次不說
        if final != text:
            logger.warning(f"[lively_evolve] 例行 #{r['id']} 写出来像起草稿，{'挑出了终稿' if final else '这次不说'}:\n{text[:400]}")
        text = re.sub(r"&&[^&]*&&", "", final or "").replace("。", "").strip()
        if not text or re.search(r"@|全体成员|\d{5,}", text):
            raise RuntimeError("没写出合适的话")
        umo = (self.s.group_umo(r["gid"]) if r["action"] == "group_say"
               else self.s.private_umo(self.s.owner()))
        await self.context.send_message(umo, MessageChain().message(text))
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo) or await cm.new_conversation(umo, platform_id=self.s.platform_id())
            await cm.add_message_pair(cid, {"role": "user", "content": self.s.fill("（系统记录：〈我〉按自己的例行安排主动说话）")},
                                      {"role": "assistant", "content": text})
        except Exception as e:
            logger.warning(f"[lively_evolve] 写入聊天记录失败: {e}")
        logger.info(f"[lively_evolve] 例行 #{r['id']} {r['name']}: {text}")
        return text

    # ---------- 三級：每週五提案 ----------
    def _week_wishes(self, dropped: bool = False) -> list[dict]:
        """這週的願望；dropped=True 時改成返回被模型篩掉的"""
        since = max(time.time() - 7 * 86400, self.state.get("last_propose_ts", 0))
        groups = self._groups()
        out = []
        for w in self._jsonl(self.wish_file, since):
            if (w.get("check") == "no") != dropped:
                continue
            who = "〈主人〉" if self.s.is_owner(w.get("qq")) else (w.get("name") or w.get("qq"))
            where = groups.get(w.get("gid"), "私聊") if w.get("gid") else "私聊"
            item = {"什么时候": datetime.fromtimestamp(w.get("ts") or 0, TZ).strftime("%m-%d %H:%M"), "谁": who, "在哪": where, "怎么记下的": {"cant": "〈我〉说做不到", "wish": "对方直接提", "tool": "〈我〉自己记的"}.get(w.get("src"), ""),
                    "原话": w.get("ask"), "〈我〉当时回": w.get("reply"), "补充": w.get("why")}
            if w.get("what"):
                item["想学"] = w["what"]
            if dropped:
                item["为什么筛掉"] = w.get("check_why", "")
            out.append(item)
        return out[-150:]

    def _capabilities(self) -> str:
        """〈我〉會的本領：按現在載入的插件現生成，關著的標出來；再加上例行動作和故意定的規矩"""
        lines = ["〈我〉现在会的本领（装着的插件）："]
        for st in self.context.get_all_stars():
            if st.reserved or not st.activated or not st.name:
                continue
            desc = re.sub(r"https?://\S+", "", str(st.desc or st.short_desc or ""))
            name = _cn_name(st) or st.display_name or st.name
            desc = re.sub(r"\s+", " ", desc).strip(" -：:")
            if desc.startswith(name):  # 很多 desc 開頭就是插件名
                desc = desc[len(name):].lstrip(" -：:")
            state = getattr(st.star_cls, "state", None)
            off = isinstance(state, dict) and state.get("enabled") is False
            lines.append(f"- {name}：{desc[:100]}{'（现在关着）' if off else ''}")
        lines.append(ROUTINE_CAPS)
        limits = [str(x).strip() for x in (self.s.get("limits") or []) if str(x).strip()]
        if self.context.get_registered_star("astrbot_plugin_lively_comic"):
            limits.insert(0, "画笔只给〈主人〉用，别人要画画会被拒绝")
        if limits:
            lines.append("故意定的规矩（不是做不到，别人提这些不算愿望）：\n" + "\n".join(f"- {x}" for x in limits))
        return self.s.fill("\n".join(lines)).translate(T2S)

    def _tools(self) -> str:
        """〈我〉手上現有的工具（10-31：只看插件說明會把已經做出來的工具又寫成提案）"""
        try:
            funcs = self.context.get_llm_tool_manager().func_list
        except Exception:
            return ""
        lines = []
        for f in funcs:
            if not getattr(f, "active", True) or not getattr(f, "name", ""):
                continue
            desc = re.sub(r"\s+", " ", str(getattr(f, "description", "") or "").split("\n")[0]).strip()
            props = ((getattr(f, "parameters", None) or {}).get("properties") or {}) if isinstance(getattr(f, "parameters", None), dict) else {}
            args = "；".join((k + " " + re.sub(r"\s+", " ", str((v or {}).get("description") or ""))[:50]).strip() for k, v in props.items())
            lines.append(f"- {f.name}：{desc[:80]}" + (f"（参数：{args[:200]}）" if args else ""))
        if not lines:
            return ""
        return self.s.fill("〈我〉手上现有的工具（有的只在〈主人〉那边或好感够的人那边看得到）：\n" + "\n".join(lines)).translate(T2S)

    async def _cover(self, scene: str) -> str | None:
        star = self.context.get_registered_star("astrbot_plugin_lively_comic")
        comic = getattr(star, "star_cls", None)
        if comic is None:
            return None
        look = getattr(comic, "self_look", "")
        ref = getattr(comic, "self_ref", None)
        prompt = (look + (scene or "the main character proudly presenting a long wish list scroll to their owner")
                  + ". Cute anime illustration, soft pastel colors, cozy, no text.")
        for p in comic._drawing_providers():
            try:
                data = await comic._call_images(p, prompt, ref if ref and ref.exists() else None)
                path = self.dir / f"proposal_{datetime.now(TZ):%Y%m%d_%H%M}.png"
                path.write_bytes(data)
                return str(path)
            except Exception as e:
                logger.warning(f"[lively_evolve] 封面画失败，换下一个: {p.get('model')}: {e}")
        return None

    async def propose(self, manual: bool = False) -> str:
        async with self._busy:
            week = datetime.now(TZ).strftime("%G-W%V")
            wishes = self._week_wishes()
            if not wishes:
                self.state["last_propose_week"] = week
                self.state["last_propose_ts"] = int(time.time())
                self._save()
                await self._tell(f"到了每周五整理成长提案的时候，这周没记下新愿望（{week}），没有提案", "这周没攒到愿望")
                return "这周没攒到新愿望，没有提案"
            old = [{"标题": p["title"], "结果": p.get("status"), **({"〈主人〉定的做法": p["note"]} if p.get("note") else {})}
                   for p in self.state["proposals"][-30:]]
            routines = [self._desc(r) for r in self.state["routines"]]
            prompt = (self._capabilities() + "\n\n" + self._tools() + "\n\n已有的例行动作：\n" + ("\n".join(routines) or "（没有）")
                      + "\n\n可用的群：" + json.dumps(self._groups(), ensure_ascii=False)
                      + "\n\n以前提过的提案：" + json.dumps(old, ensure_ascii=False)
                      + "\n\n本周的愿望记录：\n" + json.dumps(wishes, ensure_ascii=False))
            data = _json(await self._llm(prompt, PROPOSE_RULE, timeout=180))
            if not data:
                raise RuntimeError("模型没给出能解析的提案")

            made = []
            for r in (data.get("routines") or [])[:1]:
                added, err = self._add_routine(r, "self")
                if added:
                    made.append(f"{self._desc(added)}\n  因为：{str(r.get('why') or '')[:80]}")
                else:
                    logger.info(f"[lively_evolve] 例行动作没加上（{err}）: {r}")

            covered = [c for c in (data.get("covered") or []) if isinstance(c, dict) and c.get("wish")][:10]
            covered = [f"{str(c.get('wish'))[:60]} → 已经能做：{str(c.get('tool') or '')[:40]}，{str(c.get('how') or '')[:80]}" for c in covered]

            n0 = max([p["n"] for p in self.state["proposals"]] or [0])
            props = []
            for i, p in enumerate((data.get("proposals") or [])[:3], 1):
                props.append({"n": n0 + i, "week": week, "status": "pending", "ts": int(time.time()),
                              **{k: (p.get(k) if k == "evidence" else str(p.get(k) or "")) for k in
                                 ("title", "who", "evidence", "idea", "needs", "risk", "effort", "brief")}})
            self.state["proposals"] = (self.state["proposals"] + props)[-60:]
            self.state["last_propose_week"] = week
            self.state["last_propose_ts"] = int(time.time())
            self._save()
            self._log("proposal", text=f"本周提案 {len(props)} 个，新例行 {len(made)} 个，已经能做的 {len(covered)} 条",
                      titles=[p["title"] for p in props], covered=covered)

            cover = await self._cover(str(data.get("cover") or "")) if props else None
            facts = [f"这周（{week}）记下了 {len(wishes)} 条愿望，整理成 {len(props)} 个成长提案，在等〈主人〉看（〈主人〉同意了才会做）"]
            facts += [f"【{p['n']}】{p['title']}：{p['idea']}（{p['who']}想要）" for p in props]
            if made:
                facts.append("用现有本领就能做到的，〈我〉直接安排成了例行动作：" + "；".join(m.split("\n")[0] for m in made))
            if covered:
                facts.append("有些愿望〈我〉现在手上的工具已经能做到，没写成提案：" + "；".join(covered))
            head = [f"成长提案 · 等你定（{week}）", f"这周记下 {len(wishes)} 条愿望"]
            head += [f"【{p['n']}】{p['title']}（{p['who']}，工作量{p['effort']}）" for p in props]
            if made:
                head.append("现有本领就能做到的，直接安排成了例行动作：\n" + "\n".join(made))
            if covered:
                head.append("现在手上的工具已经能做到、没写成提案的：\n" + "\n".join(covered))
            cards = ["\n".join(head)]
            for p in props:
                ev = "\n".join(f"「{str(x)[:60]}」" for x in (p["evidence"] or [])[:3])
                cards.append(f"【{p['n']}】{p['title']}\n谁想要：{p['who']}\n{ev}\n"
                             f"想法：{p['idea']}\n要用到：{p['needs']}\n风险：{p['risk']}\n\n"
                             f"—— 给 agent 的需求书 ——\n{p['brief']}")
            if props or made or covered:
                said = await self._tell("\n".join(facts), "这周的成长提案", must=True, card=cards, image=cover)
            else:
                said = await self._tell("\n".join(facts) + "（这周的愿望都不值得提，或者以前提过）", "这周没有提案")
            self._write_md(week, {"intro": said}, props, made, covered)
            return f"提案发好了：{len(props)} 个提案，{len(made)} 个新例行"

    def _write_md(self, week: str, data: dict, props: list[dict], made: list[str], covered: list[str] = ()):
        try:
            lines = [f"# 〈名字〉成长提案 {week}", "", str(data.get("intro") or ""), ""]
            for p in props:
                lines += [f"## {p['n']}. {p['title']}", "", f"- 谁想要：{p['who']}", f"- 工作量：{p['effort']}",
                          f"- 想法：{p['idea']}", f"- 要用到：{p['needs']}", f"- 风险：{p['risk']}", "",
                          "原话：", *[f"> {x}" for x in (p["evidence"] or [])], "", "### 需求书", "", p["brief"], ""]
            if made:
                lines += ["## 直接安排的例行动作", "", *[f"- {x}" for x in made], ""]
            if covered:
                lines += ["## 已经能做的（没写成提案）", "", *[f"- {x}" for x in covered], ""]
            (self.dir / f"proposals_{week}.md").write_text(self.s.fill("\n".join(lines)), "utf-8")
        except Exception as e:
            logger.warning(f"[lively_evolve] 写提案文件失败: {e}")

    # ---------- 主人指令 ----------
    def _status(self) -> str:
        lines = [f"自我成长：{'开' if self.state['enabled'] else '关'}，每天 {REFLECT_AT} 复盘，每周五 {PROPOSE_AT} 提案"]
        lines.append("参数：" + "，".join(
            f"{spec[0]} {_fmt(k, self.tunes['values'][k])}{'🔒' if k in self.tunes['locked'] else ''}"
            for k, spec in TUNABLES.items()))
        if self.state["history"]:
            h = self.state["history"][-1]
            lines.append(f"最近一次调整：{datetime.fromtimestamp(h['ts'], TZ):%m-%d} {TUNABLES[h['key']][0]} "
                         f"{_fmt(h['key'], h['old'])} → {_fmt(h['key'], h['new'])}（{h['why']}）")
        lines.append(f"相处经验 {len(self.state['notes'])} 条，例行动作 {len([r for r in self.state['routines'] if r.get('enabled')])} 个，"
                     f"本周愿望 {len(self._week_wishes())} 条，等你回的提案 {len([p for p in self.state['proposals'] if p.get('status') == 'pending'])} 个")
        return "\n".join(lines)

    async def _owner_cmd(self, text: str) -> str | None:
        m = re.match(r"^成长\s*(.*)$", text)
        if m:
            arg = m.group(1).strip()
            if arg in ("", "状态"):
                return self._status()
            if arg in ("开", "开启"):
                self.state["enabled"] = True
                self._save()
                return "自我成长开了"
            if arg in ("关", "关闭"):
                self.state["enabled"] = False
                self._save()
                return "自我成长先停了：不复盘、不跑例行、不提案，经验也先不带"
            if arg in ("反思", "复盘"):
                return await self.reflect(datetime.now(TZ))
            if arg in ("重整经验", "重写经验"):
                if self._busy.locked():
                    return "她正在整理，等一下再试"
                async with self._busy:
                    if arg == "重写经验":  # 拿昨天的聊天按最新的规矩重写
                        r = await self.update_notes(datetime.now(TZ))
                    else:  # 只按最新的人设和规矩把现有的删改一遍
                        r = await self.align_notes(force=True)
                return r or "看过了，现在的经验和人设、规矩不冲突，不用改"
            if arg == "经验":
                return "相处经验：\n" + ("\n".join(f"{i}. {n['text']}" for i, n in enumerate(self.state["notes"], 1)) or "还没有")
            mm = re.match(r"^删经验\s*(\d+)$", arg)
            if mm:
                i = int(mm.group(1)) - 1
                if 0 <= i < len(self.state["notes"]):
                    gone = self.state["notes"].pop(i)
                    self._save()
                    self._log("notes", text=f"〈主人〉删了一条经验：{gone['text']}")
                    return f"删掉了：{gone['text']}"
                return "没有这一条"
            mm = re.match(r"^(锁|解锁)\s*(.+)$", arg)
            if mm:
                key = ALIASES.get(mm.group(2).strip())
                if not key:
                    return "可以锁的：" + "、".join(ALIASES)
                locked = set(self.tunes["locked"])
                (locked.add if mm.group(1) == "锁" else locked.discard)(key)
                self.tunes["locked"] = sorted(locked)
                self._save_tunes()
                return f"{TUNABLES[key][0]}{'锁住了，她不会自己改' if mm.group(1) == '锁' else '解锁了'}"
            if arg == "还原":
                if not self.state["history"]:
                    return "还没调过参数"
                h = self.state["history"].pop()
                self.tunes["values"][h["key"]] = h["old"]
                self.tunes["changed"][h["key"]] = int(time.time())
                self._save_tunes()
                self._save()
                self._log("tune", key=h["key"], old=h["new"], new=h["old"], why="〈主人〉还原", text=f"{TUNABLES[h['key']][0]} 还原成 {_fmt(h['key'], h['old'])}")
                return f"{TUNABLES[h['key']][0]} 还原成 {_fmt(h['key'], h['old'])} 了"
            mm = re.match(r"^学会\s*(.+)$", arg)
            if mm:
                return self._learned(mm.group(1).strip())
            if not text[2:3].isspace():
                return None  # 「成长了好多」之类的普通聊天：後面沒空格又不是認得的子指令，交給她回
            return "成长 状态/开/关/反思/经验/删经验 N/重整经验/重写经验/锁 X/解锁 X/还原/学会 描述"

        m = re.match(r"^例行\s*(.*)$", text)
        if m:
            arg = m.group(1).strip()
            if arg in ("", "列表"):
                return "例行动作：\n" + ("\n".join(self._desc(r) for r in self.state["routines"]) or "还没有")
            mm = re.match(r"^(删|停|开|试|看今天|不看今天)\s*#?(\d+)$", arg)
            if mm:
                r = next((x for x in self.state["routines"] if x["id"] == int(mm.group(2))), None)
                if not r:
                    return "没有这个例行"
                op = mm.group(1)
                if op == "删":
                    self.state["routines"].remove(r)
                elif op in ("停", "开"):
                    r["enabled"] = op == "开"
                elif op in ("看今天", "不看今天"):
                    r["today"] = op == "看今天"
                else:
                    try:
                        return "试了一下：" + (await self.run_routine(r) or "今天还没有能用的聊天，这次不说")
                    except Exception as e:
                        return f"试的时候出错了：{e}"
                self._save()
                self._log("routine", text=f"〈主人〉{op}了例行 #{r['id']} {r['name']}")
                done = {"删": "删掉了", "停": "停了", "开": "开了", "看今天": "以后先看今天的聊天再说",
                        "不看今天": "以后不看今天的聊天了"}[op]
                return f"#{r['id']} {r['name']} {done}"
            mm = re.match(r"^加\s*(.+)$", arg)
            if mm:
                groups = self._groups()
                rule = ROUTINE_PARSE_RULE.replace("{groups}", json.dumps(groups, ensure_ascii=False))
                data = _json(await self._llm(mm.group(1), rule))
                if not data or data.get("error"):
                    return f"没看懂：{data.get('error') if data else '解析失败'}"
                r, err = self._add_routine(data, "owner")
                return f"安排上了：{self._desc(r)}" if r else f"没加上：{err}"
            if not text[2:3].isspace():
                return None
            return "例行 列表/加 描述/删 N/停 N/开 N/试 N/看今天 N/不看今天 N"

        m = re.match(r"^提案\s*(.*)$", text)
        if m:
            arg = m.group(1).strip()
            if arg in ("现在", "立刻", "马上"):
                if self._busy.locked():
                    return "她正在整理，等一下再试"
                asyncio.create_task(self._propose_bg())
                return "开始整理这周的愿望了，几分钟后连封面一起发给你"
            if arg in ("", "列表"):
                ps = self.state["proposals"][-10:]
                names = {"pending": "等你回", "ok": "同意了", "no": "不要", "done": "学会了"}
                return "最近的提案：\n" + ("\n".join(f"【{p['n']}】{p['title']}：{names.get(p.get('status'), '')}"
                                                   + (f"（{p['note']}）" if p.get("note") else "") for p in ps) or "还没有")
            mm = DECIDE_RE.match(arg)
            if mm:  # 面板回提案走這裡；主人私信打這句不攔，交給她（lively_proposal）
                return self.decide_proposal(int(mm.group(1)), mm.group(2), mm.group(3).strip())
            if not text[2:3].isspace():
                return None
            return "提案 现在/列表（回提案直接跟她说，或者在面板信箱点）"

        if text in ("愿望", "愿望清单", "本周愿望"):
            ws, gone = self._week_wishes(), len(self._week_wishes(dropped=True))
            tail = f"\n另外筛掉了 {gone} 条（回「愿望 筛掉」看）" if gone else ""
            if not ws:
                return "这周还没记下愿望" + tail
            return f"这周记下 {len(ws)} 条（最近 10 条）：\n" + "\n".join(
                f"- {w['谁']}（{w['在哪']}）：{w.get('想学') or str(w['原话'])[:50]}" for w in ws[-10:]) + tail
        if text in ("本领", "能力", "能力清单"):
            return self._capabilities()
        if re.match(r"^愿望\s*筛掉$", text):
            ws = self._week_wishes(dropped=True)
            if not ws:
                return "这周没筛掉什么"
            return f"这周筛掉 {len(ws)} 条（最近 10 条）：\n" + "\n".join(
                f"- {w['谁']}：{str(w['原话'])[:40]}（{w['为什么筛掉']}）" for w in ws[-10:])
        return None

    def _learned(self, what: str, n: int | None = None) -> str:
        self.state["learned"] = (self.state["learned"] + [{"text": what[:100], "ts": int(time.time()), "n": n}])[-50:]
        self._save()
        self._log("learned", text=what[:100])
        return f"记下了学会的本领：{what[:100]}"

    def decide_proposal(self, n: int, word: str, note: str = "") -> str:
        """記下主人對提案的決定（面板、她的工具都走這裡）：好／不要／做好了，note 是主人定的做法或為什麼不要。回傳只講事實"""
        p = next((x for x in self.state["proposals"] if x["n"] == n), None)
        if not p:
            return f"没有提案【{n}】"
        word = str(word or "").strip().translate(T2S)
        note = str(note or "").strip()
        if any(w in word for w in ("做好", "完成", "学会")):
            p["status"] = "done"
            self._save()
            return f"提案【{n}】{p['title']}记成做好了；" + self._learned(note or p["title"], n)
        if p.get("status") == "done":
            return f"提案【{n}】{p['title']}已经做好了，不用再定"
        p["status"] = "no" if any(w in word for w in ("不", "算了", "别")) else "ok"
        if note:  # 〈主人〉定的做法记在提案上，之后整理提案、被问到成长时都带着
            p["note"] = note[:300]
        self._save()
        self._log("proposal", text=f"〈主人〉{'没同意' if p['status'] == 'no' else '同意了'}提案【{p['n']}】{p['title']}"
                  + (f"：{note[:300]}" if note else ""))
        if p["status"] == "no":
            return f"提案【{n}】{p['title']}记成不要了，以后不再提" + (f"（为什么：{note[:300]}）" if note else "")
        return f"提案【{n}】{p['title']}记成同意了" + (f"，做法：{note[:300]}" if note else "") + "；需求书在那周提案的卡片里"

    async def _propose_bg(self):
        try:
            msg = await self.propose(manual=True)
            logger.info(f"[lively_evolve] {msg}")
        except Exception as e:
            logger.error(f"[lively_evolve] 提案失败: {e}", exc_info=True)
            await self._tell(f"整理这周的成长提案时出错了：{str(e)[:100]}", "整理提案出错", must=True,
                             card=f"整理成长提案出错：{str(e)[:100]}")

    # ---------- 主人回提案：主人怎麼說都交給她（10-26 用戶定不做固定指令） ----------
    @filter.on_llm_request(priority=100)
    async def hide_proposal(self, event: AstrMessageEvent, req):
        tools = getattr(req, "func_tool", None)
        if not tools or self.s.is_owner(str(event.get_sender_id() or "")):
            return
        try:
            if tools.get_tool("lively_proposal"):
                tools.remove_tool("lively_proposal")
        except Exception:
            pass

    @filter.llm_tool("lively_proposal")
    async def tool_proposal(self, event: AstrMessageEvent, n: int, action: str, note: str = ""):
        """记下主人对某个成长提案的决定：好（同意做）、不要、做好了（已经实现、你学会了）。只有主人说了才做得成；主人怎么回没有固定说法，你自己理解。
        现在等主人回的提案、主人以前定的做法，主人问起成长时你都看得到。

        Args:
            n(number): 提案编号（【N】里的数字）
            action(string): 好、不要、做好了
            note(string): 主人说的做法（同意时）或为什么不要；做好了时可以写学会了什么。没说就不写
        """
        if not self.s.is_owner(str(event.get_sender_id() or "")):
            return self.s.fill("没记：提案的决定要〈主人〉说了才算")
        try:
            return self.s.fill(self.decide_proposal(int(n), action, note))
        except Exception as e:
            logger.error(f"[lively_evolve] 记提案决定失败: {e}", exc_info=True)
            return f"没记成：{str(e)[:80]}"

    # ---------- 小工具箱：重要改動先問 Claude 姐姐（10-30） ----------
    @filter.llm_tool("lively_toolbox")
    async def tool_toolbox(self, event: AstrMessageEvent, action: str = "看", name: str = "", desc: str = "", code: str = "",
                           path: str = "", lang: str = "python", usage: str = "", test: str = "", args: str = "", why: str = ""):
        """你自己的小工具箱：把你在自己电脑上写好、试过的程序收进来，以后用名字直接拿来跑。
        收进工具箱是重要改动，要先交给 Claude 姐姐看，她同意了才收得进来；她没同意会说为什么，你可以改了再交。她回了会告诉你。
        收进来的是交的那一版，电脑上的文件之后改了不算，要换新版就再交一次（等她回之前照旧用旧版）。
        跑工具和 lively_pc_run 一样：只有〈主人〉和你好感度 70 以上的人叫时跑得了，算电脑的步数。参数在程序里是字符串 ARGS（powershell 是 $ARGS_TEXT），程序自己解析。
        撤掉工具你自己定，不用审。〈主人〉说直接放行或不放行时可以用 放行、不放行（只有〈主人〉说了才做得成）。

        Args:
            action(string): 看、交、用、撤、放行、不放行
            name(string): 工具的名字（交、用、撤、放行、不放行时要写）
            desc(string): 交的时候写：这个工具做什么，一句话
            code(string): 交的时候：程序本身；已经存在电脑上的就不用写，写 path
            path(string): 交的时候：程序在 pc 里的路径，例如 work/汇率.py
            lang(string): python（默认）、node 或 powershell
            usage(string): 交的时候写：怎么用、ARGS 写什么
            test(string): 交的时候写：你试跑过的情况和输出，Claude 姐姐会看
            args(string): 用的时候传给程序的参数
            why(string): 交的时候写为什么想收进来；用的时候写为什么用（记在记录里）
        """
        act = str(action or "看").strip().translate(T2S)
        try:
            if act in ("看", "列", "list"):
                return self.s.fill(self.toolbox.list_text())
            if act in ("交", "送审", "提交", "收"):
                return self.s.fill(await self.toolbox.submit(event, name, desc, code, path, lang, usage, test, why))
            if act in ("用", "跑", "run"):
                return self.s.fill(await self.toolbox.use(event, name, args, why))
            if act in ("撤", "删", "撤掉"):
                return self.s.fill(self.toolbox.remove(name))
            if act in ("放行", "不放行"):
                if not self.s.is_owner(str(event.get_sender_id() or "")):
                    return self.s.fill("没做：直接放行、不放行要〈主人〉说了才算，别人叫的做不成")
                rid = self.toolbox.rid_of(name)
                if not rid:
                    return f"「{name}」现在没有在等 Claude 姐姐看的"
                verdict = "pass" if act == "放行" else "reject"
                return self.s.fill(await self.review.decide(rid, verdict, why or "", by="〈主人〉"))
            return "没做：action 只认 看、交、用、撤、放行、不放行"
        except Exception as e:
            logger.error(f"[lively_evolve] 工具箱出错: {e}", exc_info=True)
            return f"没做成：{str(e)[:80]}"

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=1350)
    async def owner(self, event: AstrMessageEvent):
        if not self.s.is_owner(event.get_sender_id()):
            return
        text = (event.message_str or "").strip().translate(T2S)
        if not re.match(r"^(成长|例行|提案|愿望|本领|能力)", text):
            return
        m = re.match(r"^提案\s*(.*)$", text)
        if m and DECIDE_RE.match(m.group(1).strip()):
            return  # 回提案不攔成指令，交給她
        try:
            reply = await self._owner_cmd(text)
        except Exception as e:
            logger.error(f"[lively_evolve] 指令出错: {e}", exc_info=True)
            reply = f"出错了：{str(e)[:100]}"
        if reply is not None:
            event.stop_event()
            await event.send(MessageChain().message(self.s.admin_text(reply)))
