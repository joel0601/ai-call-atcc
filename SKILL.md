---
name: ai-call-atcc
description: "扫描钉钉群机器人「AI外呼转人工」分配消息，提取用户ID和分配CC，在群内发真@（带红点通知）消息提醒CC跟进。覆盖51talk台湾/泰国/港澳三个市场。当用户想查'AI外呼机器人分配给CC的用户'、做每日AI外呼跟进提醒、定时扫描钉钉群机器人分配消息并@CC、或提到at_cc/外呼@CC/AI外呼分单提醒时使用。"
---

# AI 外呼 @CC 自动化

扫描钉钉群里机器人发的「AI外呼转人工」分配消息，提取用户ID和分配CC，在群内发**真@**（带红点通知）消息提醒CC跟进。

## 前置依赖

- **dws CLI**（dingtalk-operations skill）：用于拉群成员、查消息、发消息。执行前先 `dws auth status` 确认登录态；失效时引导用户扫码登录。
- **pypinyin**：脚本启动时自动检测并安装（环境重启后可能丢失）。

## 核心脚本

`scripts/at_cc.py` — 单文件完成多群扫描、CC解析、真@发送、四重去重。

### 用法

```bash
# 单群，中文回复
python3 scripts/at_cc.py --group <cid> --hours 1.0

# 多群联合扫描
python3 scripts/at_cc.py --group <cid1> --group <cid2> --hours 1.0

# 指定英文回复语（泰国群，群内@模式）
python3 scripts/at_cc.py --group "<cid>|Please follow." --hours 1.0

# 私信模式（泰国群使用）：把机器人原版内容+提醒语单聊发给CC
python3 scripts/at_cc.py --group "<cid>|please follow up" --hours 1.0 --dm

# 只解析不发送
python3 scripts/at_cc.py --group <cid> --hours 1.0 --dry-run

# 忽略去重重跑
python3 scripts/at_cc.py --group <cid> --hours 1.0 --force
```

### 参数

| 参数 | 说明 |
|---|---|
| `--group` | 群 openConversationId，可重复传。格式 `cid` 或 `cid\|回复语`（如 `cid\|Please follow.`） |
| `--hours` | 回溯小时数，默认 0.2。定时任务建议 1.0（覆盖 agent 启动延迟） |
| `--dm` | 私信模式：把机器人原版消息内容+提醒语单聊发给CC（仅泰国群使用） |
| `--dry-run` | 只解析不发送 |
| `--force` | 忽略去重记录重跑 |
| `-v` | 显示跳过的已处理消息 |

## 已知群配置

| 市场 | 群 cid | 机器人 | 提醒方式 | 回复语 |
|---|---|---|---|---|
| 台湾 | `cidg7cEDaidhZV2ZVElAHOGyg==` | 道明寺 | 群内真@ | 请跟进。 |
| 港澳 | `cidTrTXRhb44Vj9kvCq+H11vg==` | 陈冠希 | 群内真@ | 请跟进。 |
| 泰国 | `cidfjvLwZcVixOuCMPbqfpe8g==` | Tony Jaa | **私信** | please follow up |

## 真 @ 机制（不可省略）

发送真@必须同时满足两件套：
1. `--at-open-dingtalk-ids <openDingtalkId>` 参数
2. 正文里包含 `<@openDingtalkId>` 占位符

再加 `--uuid atcc-<msg_id>` 作为服务端幂等键，防止重复发送。

消息格式：
- 中文：`（用户ID），@CC名，请跟进。`
- 英文：`（用户ID），@CC名 Please follow.`

## 私信模式（泰国群专用）

泰国群不使用群内@，改为私信提醒CC。使用 `--dm` 参数启用。

私信内容 = 机器人发送的原版消息内容 + 空行 + 提醒语（`please follow up`）

发送方式：
- `--open-dingtalk-id <cc_oid>` 单聊接收人
- `--uuid atcc-dm-<msg_id>` 幂等键（与群内@的 `atcc-<msg_id>` 区分）

注意：私信模式下不会在群内发送任何消息，CC 只会收到单聊提醒。

### CC 总群（主要查找来源）

私信模式下，CC 不需要在泰国群里，但所有 CC 都在「All CC Team -Thailand 2026」总群里。

- 总群 cid：`cidXe6JdWz+VDELRVKtAxJAxA==`（73人）
- 脚本自动合并泰国群 + CC 总群成员，在合并列表中查找 CC
- 例如 `yuyang` → 于洋（Ryan CC Operation），即使他不在泰国群也能找到

### CC 映射表（补充方案，CC 总群里也找不到时）

如果某个 CC 不在 CC 总群里，手动添加到映射表。

维护文件：`state/cc_map.json`

```json
{
  "xuanjingsheng": {"oid": "DWOrlM3pwwf...", "name": "宣景胜"}
}
```

- key：CC 账号去前缀后小写（如 `THCC-xuanjingsheng` → `xuanjingsheng`）
- value：`oid`（openDingtalkId）和 `name`（显示名）
- 新 CC 不在群里时，手动添加到这个文件

## CC 解析多级策略

**成员来源**：群内@模式用扫描群成员；私信模式自动合并「扫描群 + CC 总群（All CC Team -Thailand 2026）」成员。

按顺序尝试，命中即返回：
1. **别名展开**：Deven→黄勇兴、Liam→李冠清（脚本内 ALIASES 字典）
2. **CC 映射表**：查 `state/cc_map.json`（补充方案，CC 总群里也找不到时）
3. **去前缀**：去掉 `51` / `THCC-` 前缀
4. **精确匹配**：昵称整体 / 括号内容 / 真名归一化
5. **切词匹配**：按空格切词，短英文名如 `THCC-pang` → `Pang Jennisa`
6. **拼音匹配**：中文真名→全拼（pypinyin）
7. **去尾部数字**：`51kangxianghua001` → `kangxianghua` 再匹配
8. **模糊包含**：长度≥6时子串匹配，仅唯一命中才返回

**安全阀**：拉群成员失败或返回空列表时，中止该群处理，绝不发纯文本@（无红点）。

## 四重去重

1. **消息ID键**：`{group}|{openMessageId}`
2. **业务键**：`{group}|biz|{user_id}|{cc}|{date}`
3. **服务端幂等键**：`--uuid atcc-{msg_id}`
4. **文件锁**：`state/run.lock` 防止并发执行

去重表持久化在 `state/processed.json`，最近执行结果在 `state/last_run.json`。

## 三市场排期

### 台湾
| 时段 | 类型 | 星期 |
|---|---|---|
| 10:00 | 转介绍 | 周六 |
| 11:00 | 非转介绍 | 周六 |
| 11:30 | 非转介绍 | 工作日 |
| 12:30 | 转介绍 | 工作日 |
| 14:00 | 转介绍 | 周日 |
| 15:00 | 非转介绍 | 周日 |
| 19:00 | 非转介绍 | 工作日 |
| 20:00 | 转介绍 | 工作日 |

### 泰国
| 时段 | 类型 | 星期 |
|---|---|---|
| 11:00 | 非转介绍（额外） | 周五 |
| 14:00 | 非转介绍+转介绍 | 周一二四五六日（周三停） |
| 20:00 | 非转介绍+转介绍 | 周一二四五六日（周三停） |

### 港澳
| 时段 | 类型 | 星期 |
|---|---|---|
| 14:00 | 非转介绍 | 周一三四五六日（周二停） |
| 19:00 | 非转介绍 | 周一三四五六日（周二停） |

**注意**：港澳 19:00 批次消息推送慢，通常 19:16-19:21 才到达，需 19:30 兜底扫描。

## 定时任务建议

用 2 个定时任务覆盖全部排期（cron 表达式按用户时区）：

1. **中文群（港澳+台湾）**：`15,30,45 10-12,14-15,19-20 * * *`
   - 扫描台湾+港澳群，群内真@，中文回复"请跟进。"
2. **泰国群（英文）**：`15 11,14,20 * * *`
   - 扫描泰国群，**私信模式**（`--dm`），把机器人原版内容私信发给CC，末尾加"please follow up"

每个任务 query 开头必须写：`本次请求是由「{title}」定时任务到时触发的。`

## 执行流程

1. `dws auth status` 确认登录态（失效则引导扫码）
2. 执行 `python3 scripts/at_cc.py --group ... --hours 1.0`（泰国群加 `--dm`）
3. 解析输出：`[OK]` 为成功发送（群内@或私信）、`[WARN]` 为CC未解析、`[FAIL]` 为发送失败、`[SKIP]` 为已处理
4. 报告各群命中/发送/失败数，列出未解析的CC账号
5. 若 dws 登录态失效，告知用户需重新扫码登录

## 常见问题

- **CC未解析**：检查群成员昵称是否变更，或在脚本 ALIASES 字典中添加别名映射
- **消息漏抓**：agent启动延迟约3-8分钟，`--hours` 建议设为 1.0
- **重复@**：检查 `state/processed.json` 是否被意外删除，或 `--uuid` 幂等键是否生效
- **环境重启后**：dws 登录态（`~/.dws/.data`）和 pypinyin 可能丢失，需重新扫码登录；脚本会自动重装 pypinyin
