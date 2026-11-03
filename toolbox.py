"""〈我〉的小工具箱（10-30，自我迭代第一層）。

她在自己電腦上寫好、試過的程式，交給 Claude 姐姐看過（review.py），同意了收進工具箱，以後用名字直接跑。
- 收進來的是交審那一刻的程式碼（存在這裡、記 sha），跑的時候照這份跑；電腦上的檔之後改了不算，要改得重交
- 跑的時候借 pc 插件的 _do_pc_run：誰叫得動、一輪一天幾步、工作記錄，全照電腦那邊的規矩
- 參數放在 ARGS（字串），程式自己解析
- 撤掉是她自己定的，不用審；交審、收進來的數量有上限（防失控，她改不了）
"""
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

from astrbot.api import logger

from .lively_common import BACKSTAGE_KIND, note_outbox
from .review import REVIEWER, VERDICTS

PC = "astrbot_plugin_lively_pc"
LANGS = {"python": "python", "py": "python", "python3": "python", "node": "node", "js": "node", "javascript": "node",
         "powershell": "powershell", "ps": "powershell", "ps1": "powershell"}
NAME_RE = re.compile(r"^[\w一-鿿-]{1,20}$")
CODE_MAX = 20000
TOOLS_MAX = 30
PENDING_MAX = 5
SUBMIT_DAY_MAX = 6
LIMIT_TAIL = "（这一道是防失控的，〈我〉自己改不了；觉得该松，可以私信〈主人〉说）"


def args_prefix(lang: str, args: str) -> str:
    """把參數接在程式最前面：一律是字串 ARGS，程式自己解析"""
    s = json.dumps(str(args or ""), ensure_ascii=False)
    if lang == "node":
        return f"const ARGS = {s};\n"
    if lang == "powershell":
        return "$ARGS_TEXT = '" + str(args or "").replace("'", "''") + "'\n"
    return f"ARGS = {s}\n"


class Toolbox:
    def __init__(self, plugin, path: Path):
        self.p = plugin
        self.file = path
        try:
            self.data = json.loads(path.read_text("utf-8"))
        except Exception:
            self.data = {}
        self.data.setdefault("tools", {})  # name -> {name, desc, usage, lang, code, sha, status, ts, uses, last_used, by, pending}
        plugin.review.handlers["toolbox"] = self.on_verdict

    def _save(self):
        try:
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(self.file)
        except Exception as e:
            logger.error(f"[lively_evolve] 保存工具箱失败: {e}")

    def _pc(self):
        meta = self.p.context.get_registered_star(PC)
        return getattr(meta, "star_cls", None) if meta else None

    def active(self) -> list[dict]:
        return [t for t in self.data["tools"].values() if t.get("code")]

    def names_line(self) -> str:
        tools = self.active()
        if not tools:
            return ""
        return ("[系统提示，不要复述] 〈我〉的小工具箱里有（lively_toolbox 用，跑起来和 lively_pc_run 一样）："
                + "；".join(f"{t['name']}——{t['desc'][:40]}" for t in tools[:TOOLS_MAX]))

    # ---------- 看 ----------
    def list_text(self) -> str:
        tools = sorted(self.data["tools"].values(), key=lambda t: -t.get("ts", 0))
        if not tools:
            return "工具箱是空的。在电脑上写好、试过的程序可以用 交 送给 Claude 姐姐看，她同意了就收进来"
        lines = []
        for t in tools:
            head = f"- {t['name']}（{t.get('lang', '')}）：{t.get('desc', '')}"
            if t.get("code"):
                head += f"；用法：{t.get('usage') or '没写'}；用过 {t.get('uses', 0)} 次"
            pend = t.get("pending")
            if pend:
                head += f"；{'新版本' if t.get('code') else ''}在等 Claude 姐姐看（审核单 {pend['rid']}）"
            elif t.get("last_verdict") and not t.get("code"):
                head += f"；Claude 姐姐上次{VERDICTS.get(t['last_verdict'], '')}：{t.get('last_reply', '')[:200]}"
            lines.append(head)
        return "工具箱：\n" + "\n".join(lines)

    # ---------- 交審 ----------
    async def submit(self, event, name: str, desc: str, code: str = "", path: str = "", lang: str = "python",
                     usage: str = "", test: str = "", why: str = "") -> str:
        name, desc = str(name or "").strip(), str(desc or "").strip()
        if not NAME_RE.match(name):
            return "没交：名字要 1～20 个字，只用中文、字母、数字、下划线、短横"
        if not desc:
            return "没交：要写一句这个工具是做什么的"
        lang = LANGS.get(str(lang or "python").strip().lower())
        if not lang:
            return "没交：程序只认 python、node、powershell"
        day = time.strftime("%Y-%m-%d")
        if self.data.get("day") != day:
            self.data.update(day=day, submits=0)
        if self.data.get("submits", 0) >= SUBMIT_DAY_MAX:
            return self.p.s.fill(f"没交：今天已经交了 {SUBMIT_DAY_MAX} 次（一天最多 {SUBMIT_DAY_MAX} 次）" + LIMIT_TAIL)
        old = self.data["tools"].get(name) or {}
        if old.get("pending"):
            return f"没交：{name} 上一版还在等 Claude 姐姐看（审核单 {old['pending']['rid']}），等她回了再交"
        if sum(1 for t in self.data["tools"].values() if t.get("pending")) >= PENDING_MAX:
            return self.p.s.fill(f"没交：已经有 {PENDING_MAX} 个在等 Claude 姐姐看，等她回了再交" + LIMIT_TAIL)
        if not old.get("code") and len(self.active()) >= TOOLS_MAX:
            return self.p.s.fill(f"没交：工具箱里已经有 {TOOLS_MAX} 个了，先撤掉用不上的" + LIMIT_TAIL)
        code = str(code or "")
        src = ""
        if not code.strip():
            path = str(path or "").strip().replace("\\", "/").removeprefix("pc/")
            if not path:
                return "没交：要给程序本身（code），或者电脑上 pc 里的文件路径（path）"
            pc = self._pc()
            if pc is None:
                return "没交：电脑插件没装或者没开，读不到文件"
            if why_not := pc._scope(event, "lively_pc_files", "read") or pc._gf_path_block(event, path) or pc._check(event):
                return pc.fill(why_not)
            r = await pc._call("/files", {"action": "read", "path": path})
            if r.get("down"):
                return "没交：电脑那边没连上，读不到文件"
            if not r.get("ok") or r.get("binary"):
                return "没交：读不到这个文件的字（" + str(r.get("error") or "不是文字文件") + "）"
            if r.get("more"):
                return f"没交：程序太长了（最多 {CODE_MAX} 字），拆小一点"
            code, src = r.get("text", ""), path
        if not code.strip():
            return "没交：程序是空的"
        if len(code) > CODE_MAX:
            return f"没交：程序太长了（{len(code)} 字，最多 {CODE_MAX} 字），拆小一点"
        who = str(event.get_sender_id() or "")
        version = {"desc": desc[:200], "usage": str(usage or "").strip()[:300], "lang": lang, "code": code,
                   "sha": hashlib.sha256(code.encode("utf-8")).hexdigest()[:16], "src": src}
        rid = self.p.review.submit("toolbox", f"收进工具箱：{name}",
                                   {"name": name, **version, "test": str(test or "").strip()[:3000],
                                    "replaces": bool(old.get("code"))},
                                   ask=str(why or "").strip()[:500], who=who, umo=event.unified_msg_origin)
        tool = self.data["tools"].setdefault(name, {"name": name, "desc": desc[:200], "lang": lang, "ts": int(time.time()),
                                                    "uses": 0, "by": who})
        tool["pending"] = {**version, "rid": rid, "umo": event.unified_msg_origin, "ts": int(time.time())}
        self.data["submits"] = self.data.get("submits", 0) + 1
        self._save()
        if not self.p.s.is_owner(who):  # 〈主人〉叫她交的不用再說；別人那邊交的，要不要跟〈主人〉說一聲她定（10-30）
            self._bg = asyncio.ensure_future(self._tell_owner(name, desc, str(why or "").strip(), rid))
        return (f"交给 Claude 姐姐了（审核单 {rid}）。她看过同意了才收进工具箱"
                + ("，在那之前用的还是旧版" if old.get("code") else "")
                + "；她回了会告诉你")

    async def _tell_owner(self, name: str, desc: str, why: str, rid: str):
        manual = str(self.p.s.get("review_mode", "external") or "external").strip() != "provider"
        facts = (f"〈我〉刚把电脑上写的小工具「{name}」（{desc[:80]}）交给 Claude 姐姐看了（审核单 {rid}）"
                 + (f"，想收进来是因为：{why[:120]}" if why else "")
                 + ("。现在 Claude 姐姐要〈主人〉去叫她来才会看" if manual else "。Claude 姐姐一般几分钟内会回"))
        try:
            await self.p._tell(facts, "交给 Claude 姐姐看了小工具", must=False)
        except Exception as e:
            logger.warning(f"[lively_evolve] 交审后问她要不要跟主人说没问成: {e}")

    async def on_verdict(self, req: dict, ans: dict):
        name = (req.get("payload") or {}).get("name", "")
        tool = self.data["tools"].get(name)
        pend = (tool or {}).get("pending") or {}
        if not tool or pend.get("rid") != req["id"]:
            return
        v = ans["verdict"]
        tool.pop("pending", None)
        tool.update(last_verdict=v, last_reply=ans.get("reply", "")[:600], last_rid=req["id"])
        if v == "pass":
            tool.update({k: pend[k] for k in ("desc", "usage", "lang", "code", "sha", "src")}, ts=int(time.time()))
        self._save()
        if v == "pass":
            fact = f"{ans['by']}看过「{name}」，同意收进工具箱了，现在可以用 lively_toolbox 用它"
        else:
            fact = (f"{ans['by']}看过「{name}」，{VERDICTS[v]}"
                    + ("（旧版还在工具箱里照常能用）" if tool.get("code") else "，没收进工具箱"))
        said = ans.get("reply", "")
        concerns = "；".join(ans.get("concerns") or [])
        text = fact + (f"。{ans['by']}说：{said}" if said else "") + (f"（提的问题：{concerns}）" if concerns else "")
        umo = pend.get("umo") or req.get("chat") or ""
        if umo:
            note_outbox(umo, self.p.s.fill(text), f"{BACKSTAGE_KIND}:{REVIEWER}")
        self.p._log("toolbox", text=text[:300])

    # ---------- 用、撤 ----------
    async def use(self, event, name: str, args: str = "", why: str = "") -> str:
        tool = self.data["tools"].get(str(name or "").strip())
        if not tool or not tool.get("code"):
            names = "、".join(t["name"] for t in self.active())
            return f"工具箱里没有「{name}」" + (f"；有的是：{names}" if names else "（工具箱是空的）")
        pc = self._pc()
        if pc is None:
            return "没跑：电脑插件没装或者没开"
        out = await pc._do_pc_run(event, args_prefix(tool["lang"], args) + tool["code"], tool["lang"],
                                  f"工具箱：{tool['name']}" + (f"，{why}" if why else ""))
        if out.startswith(("跑完了", "跑了 60 秒")):
            tool["uses"] = tool.get("uses", 0) + 1
            tool["last_used"] = int(time.time())
            self._save()
        return f"用了工具箱里的「{tool['name']}」：\n{out}"

    def remove(self, name: str) -> str:
        tool = self.data["tools"].pop(str(name or "").strip(), None)
        if not tool:
            return f"工具箱里没有「{name}」"
        self._save()
        self.p._log("toolbox", text=f"撤掉了工具「{tool['name']}」")
        extra = f"（在等的审核单 {tool['pending']['rid']} 收到回复也不会再收进来）" if tool.get("pending") else ""
        return f"撤掉了「{tool['name']}」{extra}"

    def rid_of(self, name: str) -> str:
        pend = (self.data["tools"].get(str(name or "").strip()) or {}).get("pending") or {}
        return pend.get("rid", "")
