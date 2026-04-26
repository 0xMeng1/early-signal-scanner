# 费率转负+OI涨 实时扫描器
# Funding Rate Flip + OI Rising Scanner

## 这是什么？
自动监控币安500+合约，当费率刚从正变负（空头涌入）且OI在涨（真金白银建仓）时，秒推Telegram。

回测3天：前10信号平均最高涨+67%，抓到KAT +206%、APE +147%。

## 原理
```
每5分钟扫一次全市场费率
  上次: +0.005% (正常)
  这次: -0.46%  (空头突然涌入!)
  + OI在涨     (有人在建仓)
  = 推送信号
```

## 30秒安装

### 1. 配置Telegram推送
```bash
# 创建 .env.oi 文件（和脚本放同一目录）
TG_BOT_TOKEN=你的bot_token
TG_CHAT_ID=你的chat_id
```

获取方法：
- Bot Token: 找 @BotFather 创建bot
- Chat ID: 给bot发消息后访问 https://api.telegram.org/bot你的token/getUpdates

### 2. 运行
```bash
# 需要Python3 + requests库
pip install requests

# 第一次运行（存快照，不会推送）
python3 oi_funding_scanner.py

# 第二次运行（对比快照，有信号就推）
python3 oi_funding_scanner.py
```

### 3. 设置定时任务（每5分钟自动跑）

**Linux/Mac:**
```bash
crontab -e
# 加入这行:
*/5 * * * * cd /你的路径 && python3 oi_funding_scanner.py >> scanner.log 2>&1
```

**Windows:**
用任务计划程序，每5分钟跑一次 `python oi_funding_scanner.py`

## 推送效果
```
[ 费率刚转负+OI涨 ] 04-25 13:17

KAT
  价格: 0.0100  24h: +5.3%
  费率: +0.0050% -> -0.4602%
  OI: +19.1%  (8.2M > 9.0M > 9.5M > 9.8M)
  成交额: $203.7M
  市值: $63.0M  现货: 有
  广场: 1825帖 / 645K浏览
```

## 成本
$0。全部用币安公开API，无需API Key，无需付费。

## 文件说明
- `oi_funding_scanner.py` — 主脚本（唯一需要的文件）
- `.env.oi` — TG配置（你自己创建）
- `fr_snapshot.json` — 自动生成的快照文件
