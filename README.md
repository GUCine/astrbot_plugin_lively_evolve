# 自我成長

> 自我成長：按大家的反應微調參數、總結相處經驗、例行動作、每週五把群友願望整理成提案私信主人

AstrBot 插件，屬於「擬真群友」（lively）插件套件。適用 AstrBot >=4.29.0 + NapCat（aiocqhttp）。

主人、群、機器人名字、人設用詞這些共用設定在 [astrbot_plugin_lively_gate](https://github.com/GUCine/astrbot_plugin_lively_gate) 裡填，建議一起裝。

## 功能

```text
機器人的自我成長。

一級（自動）：每天 REFLECT_AT 回看昨天
  - 按大家的反應微調自己的幾個參數（語音頻率、主動找人頻率、說說草稿頻率），寫進 tunables.json，各插件用的時候讀
  - 從大家對機器人的反應裡總結幾條相處經驗，之後聊天時帶上
二級（自動）：例行動作，只能用白名單裡的現有能力（群裡定時說句話 / 寫說說草稿 / 私信主人）
三級（主人審）：平時記下「機器人做不到但有人想要」的願望，每週五 PROPOSE_AT 整理成提案，配一張圖私信主人；
  用現有能力就能做到的直接做成例行動作，做不到的寫成給 Claude 等 agent 的需求書，主人拿去實現
主人私信指令：成長 狀態/開/關/反思/經驗/刪經驗 N/鎖 X/解鎖 X/還原/學會 描述，例行 列表/加 描述/刪 N/停 N/開 N/試 N，
  提案 現在/列表/N 好/N 不要/N 做好了 [描述]，願望
```

## 依賴

參數會被 astrbot_plugin_lively_voice / astrbot_plugin_lively_proactive / astrbot_plugin_lively_qzone 讀取；週五提案的封面圖用 astrbot_plugin_lively_comic 的畫圖配置。

## 配置

在 AstrBot WebUI 的插件配置裡填，或者直接編輯 `data/config/astrbot_plugin_lively_evolve_config.json`。留空的公共項（主人、群、機器人名字、平台）會用 astrbot_plugin_lively_gate 裡填的。

| 配置項 | 說明 | 預設 |
| --- | --- | --- |
| `provider_id` | 覆盤、整理提案用的模型：留空用 AstrBot 預設模型 | （空） |
| `groups` | 參與覆盤、可以安排例行發言的群：留空就用門禁插件裡填的群 | （空） |
| `owner_qq` | 主人 QQ（留空用門禁插件裡填的） | （空） |

## 資料

執行資料在 `data/plugin_data/astrbot_plugin_lively_evolve/`。

## 許可

MIT，見 [LICENSE](LICENSE)。
