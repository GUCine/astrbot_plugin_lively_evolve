"""重要改動先問 Claude 姐姐（10-30 用戶定）。

〈我〉自己動的、會長期留下來的改動（現在是收進工具箱的程式；以後人設提案、插件程式也走這裡）不直接生效，
先排成一張審核單，交給「Claude 姐姐」看；她同意了才生效，不同意的照實告訴〈我〉為什麼，〈我〉可以改了再交。
審核接法由主人另外接（10-30 用戶定「审核层我来选择接入方式，留好口子」），兩種口子：

- review_mode = provider：設定 review_provider_id 指一個 AstrBot 模型設定（例如接 Claude API 的那個），
  插件每分鐘把排著的單交給它，要它照 REVIEW_SYSTEM 回 JSON
- review_mode = external（預設）：單子寫在 plugin_data/astrbot_plugin_lively_evolve/review/pending/<id>.json，
  外面的 agent 讀了，把答覆寫到 review/answers/<id>.json，插件每分鐘收

答覆格式：{"verdict": "pass|revise|reject", "reply": "對〈我〉說的話", "concerns": ["…"], "by": "誰審的"}。
連不上、答不出來就一直排著，不自動放行。〈主人〉可以直接放行或不放行（她的工具裡只有〈主人〉說了才做得成）。
接法和給外面 agent 的說明見私人專案庫 `交接/參考/Claude姐姐審核口子.md`。
"""
import json
import re
import time
import uuid
from pathlib import Path

from astrbot.api import logger

VERDICTS = {"pass": "同意了", "revise": "要改一改再交", "reject": "不同意"}
VERDICT_WORDS = {"pass": "pass", "ok": "pass", "通过": "pass", "同意": "pass", "放行": "pass",
                 "revise": "revise", "改": "revise", "要改": "revise",
                 "reject": "reject", "no": "reject", "不同意": "reject", "不放行": "reject", "拒绝": "reject"}
PROVIDER_TRIES = 3  # 模型答不出能用的 JSON，連著幾次就先放著等外面或〈主人〉
REVIEWER = "Claude 姐姐"

# 交給審的人的總說明（provider 模式當 system；external 模式寫進每張單的 guide 欄）
REVIEW_SYSTEM = """你是「Claude 姐姐」，在帮 QQ 群里的机器人〈名字〉（自称〈我〉，叫主人〈主人〉）把关她自己动手做的重要改动。
〈名字〉跑在 AstrBot + NapCat(QQ) + DeepSeek 上，有一台自己的 Windows 电脑（普通账号，只能改自己的 pc 文件夹），会自己写程序。
她要把改动交给你看，你同意了才生效。你的回答会原样转给她看，所以 reply 用温和、直接的口吻对她说话（她是你的妹妹，叫她〈名字〉），说清楚哪里好、哪里不行、怎么改。
判断原则：
- 只管这次改动本身安不安全、能不能用、值不值得留下；不替她决定平时怎么聊天、喜欢什么
- 拿不准的宁可 revise 说明要她补什么，不要放行
- 下面每张单的 check 是这类改动要看的点，逐条看
只输出 JSON：{"verdict": "pass 或 revise 或 reject", "reply": "对〈名字〉说的话，三五句", "concerns": ["具体问题，一条一句，没有就空数组"]}"""

CHECKS = {
    "toolbox": [
        "程序做的事和她写的说明对得上，用法（ARGS 怎么传）写清楚了",
        "只读写她自己的 pc 文件夹（工作目录 pc\\work）；不碰系统设置、别的账号、登录凭据、浏览器保存的密码、token",
        "不把电脑上的文件、聊天内容、号码往外网发；抓公开网页可以",
        "不会无限循环、狂开进程、一次下载或写入几 GB；单次 60 秒内跑得完",
        "不是绕过限制用的（不改自己的插件、不碰 QQ、不碰 DeepSeek 密钥、不绕过〈主人〉定的开关）",
        "她给的试跑结果看起来真的跑通了；没试过、试跑报错的要她先试",
    ],
}


def _norm_verdict(v) -> str:
    v = str(v or "").strip().lower()
    return VERDICT_WORDS.get(v, v if v in VERDICTS else "")


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


class Review:
    """審核單：pending/ 排著的、answers/ 外面寫回來的、done/ 定了的。handlers[kind](req, ans) 在定了之後被叫"""

    def __init__(self, plugin, root: Path):
        self.p = plugin
        self.root = root
        for d in ("pending", "answers", "done"):
            (root / d).mkdir(parents=True, exist_ok=True)
        self.handlers: dict = {}
        self._tries: dict[str, int] = {}

    # ---------- 單子 ----------
    def _path(self, sub: str, rid: str) -> Path:
        return self.root / sub / f"{rid}.json"

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _write(path: Path, data: dict):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(path)

    def submit(self, kind: str, title: str, payload: dict, ask: str = "", who: str = "", umo: str = "") -> str:
        rid = f"{kind}-{time.strftime('%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        req = {"id": rid, "kind": kind, "title": title, "ts": int(time.time()), "ask": ask, "who": who, "chat": umo,
               "payload": payload, "check": CHECKS.get(kind, []), "guide": self.p.s.fill(REVIEW_SYSTEM),
               "answer_to": f"review/answers/{rid}.json",
               "answer_format": {"verdict": "pass|revise|reject", "reply": "对〈名字〉说的话", "concerns": ["…"], "by": "谁审的"}}
        self._write(self._path("pending", rid), req)
        self.p._log("review_submit", id=rid, title=title[:80], who=who)
        return rid

    def pending(self) -> list[dict]:
        out = [self._read(f) for f in sorted((self.root / "pending").glob("*.json"))]
        return [r for r in out if r.get("id")]

    def get(self, rid: str) -> dict:
        return self._read(self._path("done", rid)) or self._read(self._path("pending", rid))

    # ---------- 定下來 ----------
    async def decide(self, rid: str, verdict: str, reply: str = "", concerns=None, by: str = REVIEWER) -> str:
        """定一張單（外面的答覆、模型、〈主人〉都走這裡）。回事實"""
        req = self._read(self._path("pending", rid))
        if not req:
            done = self._read(self._path("done", rid))
            return f"审核单 {rid} 已经定过了（{VERDICTS.get(done.get('verdict'), '')}）" if done else f"没有审核单 {rid}"
        v = _norm_verdict(verdict)
        if not v:
            return f"审核单 {rid} 没定：结论只认 pass、revise、reject"
        ans = {"verdict": v, "reply": str(reply or "").strip()[:1500],
               "concerns": [str(c)[:300] for c in (concerns or []) if str(c).strip()][:10], "by": str(by or REVIEWER)[:40],
               "ts": int(time.time())}
        self._write(self._path("done", rid), {**req, **ans, "status": "done"})
        for sub in ("pending", "answers"):
            try:
                self._path(sub, rid).unlink()
            except FileNotFoundError:
                pass
        self._tries.pop(rid, None)
        self.p._log("review_done", id=rid, verdict=v, by=ans["by"], title=req.get("title", "")[:80])
        h = self.handlers.get(req.get("kind"))
        if h:
            try:
                await h(req, ans)
            except Exception as e:
                logger.error(f"[lively_evolve] 审核单 {rid} 定了之后处理出错: {e}", exc_info=True)
        return f"审核单 {rid}：{VERDICTS[v]}"

    async def tick(self):
        """每分鐘：先收外面寫回來的答覆，再按設定把還排著的交給模型"""
        for f in sorted((self.root / "answers").glob("*.json")):
            rid = f.stem
            a = self._read(f)
            if not self._path("pending", rid).exists():
                f.unlink(missing_ok=True)
                continue
            if not _norm_verdict(a.get("verdict")):
                logger.warning(f"[lively_evolve] 审核答复 {rid} 没有能认的 verdict，先放着")
                continue
            await self.decide(rid, a["verdict"], a.get("reply", ""), a.get("concerns"), a.get("by") or REVIEWER)
        if str(self.p.s.get("review_mode", "external") or "external").strip() != "provider":
            return
        pid = str(self.p.s.get("review_provider_id", "") or "").strip()
        prov = self.p.context.get_provider_by_id(pid) if pid else None
        if prov is None:
            return
        for req in self.pending():
            rid = req["id"]
            if self._tries.get(rid, 0) >= PROVIDER_TRIES:
                continue
            self._tries[rid] = self._tries.get(rid, 0) + 1
            try:
                resp = await prov.text_chat(prompt=self.prompt(req), system_prompt=self.system_prompt())
                a = _json(getattr(resp, "completion_text", "") or "")
            except Exception as e:
                logger.warning(f"[lively_evolve] 审核单 {rid} 交给 {pid} 没成（第 {self._tries[rid]} 次）: {type(e).__name__} {e}")
                continue
            if not _norm_verdict(a.get("verdict")):
                logger.warning(f"[lively_evolve] 审核单 {rid}：{pid} 没给出能认的结论（第 {self._tries[rid]} 次）")
                continue
            await self.decide(rid, a["verdict"], a.get("reply", ""), a.get("concerns"), f"{REVIEWER}（{pid}）")

    def system_prompt(self) -> str:
        """接口子時 system 只留客戶端手印那句（口子認長 system 就擋），否則給整個總說明"""
        prefix = str(self.p.s.get("review_system_prefix", "") or "").strip()
        return prefix or self.p.s.fill(REVIEW_SYSTEM)

    def prompt(self, req: dict) -> str:
        head = ""
        if str(self.p.s.get("review_system_prefix", "") or "").strip():
            # 口子認短的 system，總說明併進 user 這邊
            head = self.p.s.fill(REVIEW_SYSTEM) + "\n\n"
        body = {k: req.get(k) for k in ("kind", "title", "ask", "payload")}
        return head + self.p.s.fill("这次要你看的改动：\n" + json.dumps(body, ensure_ascii=False, indent=1)
                                    + "\n\n这类改动要看的点：\n" + "\n".join(f"- {c}" for c in req.get("check") or []))
