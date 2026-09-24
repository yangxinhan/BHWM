# 築心織憶機器人

把訊息中的圖片自動轉成 Discord 表情規格（128x128、≤256KB）並新增到伺服器。

## 安裝與啟動

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python bot.py
```

`.env` 內容：

```
TOKEN=你的機器人token
```

Developer Portal → Bot 需開啟 **Message Content Intent**；邀請機器人時需給予 **管理表情符號（Manage Expressions）** 權限，並勾選 `bot` 與 `applications.commands` scope。

## 指令

| 用法 | 說明 |
| --- | --- |
| `!addemoji [名稱]` + 附上圖片 | 將附件圖片轉成表情（可一次附多張，名稱會自動加上 `_1`、`_2`） |
| 回覆有圖片的訊息並輸入 `!addemoji [名稱]` | 讀取被回覆訊息中的圖片 |
| `/addemoji name:<名稱> image:<圖片>` | 斜線指令版本 |

- 沒給名稱時使用檔名；名稱中非英數字的字元會轉成 `_`。
- 靜態圖輸出 PNG，GIF / 動態 WebP 輸出動態 GIF，太大時會自動縮小尺寸。
- 使用者需要「管理表情符號」權限才能使用。
