#!/usr/bin/env python3
"""
ai-call-at-cc: 扫描钉钉群机器人「AI外呼转人工」分配消息，真 @ CC 提醒跟进。

用法:
  python3 at_cc.py --group <cid> --hours 0.2          # 单群
  python3 at_cc.py --group <cid1> --group <cid2> --hours 0.5  # 多群
  python3 at_cc.py --group <cid> --hours 0.2 --dry-run  # 只解析不发送
  python3 at_cc.py --group <cid> --hours 1 --force      # 忽略去重重跑
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

# 自举：环境重启后 pypinyin 可能丢失，自动安装
try:
    from pypinyin import lazy_pinyin
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "pypinyin", "-q"],
                   capture_output=True, timeout=120)
    from pypinyin import lazy_pinyin

WORKDIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(WORKDIR, "state")
os.makedirs(STATE_DIR, exist_ok=True)
PROCESSED_FILE = os.path.join(STATE_DIR, "processed.json")
LOCK_FILE = os.path.join(STATE_DIR, "run.lock")
LAST_RUN_FILE = os.path.join(STATE_DIR, "last_run.json")
CC_MAP_FILE = os.path.join(STATE_DIR, "cc_map.json")

# CC 总群：所有可能被分配的 CC 都在这个群里。私信模式下从这个群查找 CC（不需要 CC 在扫描群里）。
DEFAULT_CC_GROUP = "cidXe6JdWz+VDELRVKtAxJAxA=="  # All CC Team -Thailand 2026

# 英文名别名（CC 账号 → 中文真名）。直接映射真名，不依赖拼音转换。
ALIASES = {
    "deven": "黄勇兴",
    "liam": "李冠清",
}


def dws_json(*args, timeout=60):
    """调用 dws 并解析 JSON。失败抛异常。"""
    cmd = ["dws"] + list(args) + ["--format", "json", "-y"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"dws {' '.join(args[:3])} failed (rc={r.returncode}): {r.stderr[:400]}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"dws returned non-JSON: {r.stdout[:300]}")


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_cc_map():
    """加载 CC 账号 → openDingtalkId 映射表（用于不在群里的 CC，私信模式）。
    格式: {"xuanjingsheng": {"oid": "xxx", "name": "宣景胜"}, ...}
    手动维护：新 CC 不在群里时，添加到 state/cc_map.json。"""
    return load_json(CC_MAP_FILE, {})


def norm(s):
    """归一化：去空白、括号、连字符，转小写。"""
    return re.sub(r"[\s_\-()（）\[\]【】]", "", s or "").lower()


def strip_cc_prefix(cc):
    """去掉 51 / THCC- 等前缀，返回纯识别串。"""
    cc = cc.strip()
    low = cc.lower()
    if low.startswith("thcc-"):
        return cc[5:]
    if cc.startswith("51") and len(cc) > 2 and cc[2].isalpha():
        return cc[2:]
    return cc


def parse_bot_message(content):
    """
    解析机器人分配消息，返回 (user_id, cc_account) 或 None。
    兼容中文（港澳/台湾）和泰语（泰国）格式，以及换行被吞的情况。
    """
    if "AI外呼转人工" not in content and "โทรโอน" not in content:
        return None

    # --- 用户 ID ---
    # 兼容 "student（ID）"、"Eric（ID）" 等任意用户名；中文/泰语字段名
    m_uid = re.search(r"(?:用户姓名|ชื่อผู้ใช้)\s*[：:]\s*[^\s（(]+[（(]\s*(\d+)\s*[)）]", content)
    if not m_uid:
        # 兜底：直接找行内第一个（数字）
        m_uid = re.search(r"[（(]\s*(\d{6,})\s*[)）]", content)
    if not m_uid:
        return None
    user_id = m_uid.group(1)

    # --- CC 账号 ---
    # 中文：分配CC：xxx   泰语：CC ที่ได้รับจัดสรร: xxx
    m_cc = re.search(r"(?:分配CC|CC\s+ที่ได้รับจัดสรร)\s*[：:]\s*([A-Za-z0-9_\-]+)", content)
    if not m_cc:
        return None
    cc = m_cc.group(1)
    end = m_cc.end()
    # 换行被吞修复：CC 后面直接是 .（如 xuanjingsheng3.），说明末尾数字是行号，截掉
    if end < len(content) and content[end] == ".":
        cc = re.sub(r"\d+$", "", cc)

    return user_id, cc


def resolve_cc(cc_account, members):
    """
    把 CC 账号解析成群成员。
    顺序：别名展开 → 精确匹配（昵称/括号/真名）→ 拼音匹配 → 模糊包含（≥6字符）。
    返回 (openDingtalkId, display_name) 或 None。
    """
    raw = cc_account.strip()
    stripped = strip_cc_prefix(raw)
    target = norm(stripped)

    # 0. 英文名别名展开
    if target in ALIASES:
        target = ALIASES[target]

    # 0.5 CC 映射表（用于不在群里的 CC，私信模式）
    cc_map = load_cc_map()
    map_key = stripped.lower()
    if map_key in cc_map:
        entry = cc_map[map_key]
        return entry.get("oid"), entry.get("name", map_key)
    # 也尝试去尾部数字版本
    no_digits_map = re.sub(r"\d+$", "", map_key)
    if no_digits_map and no_digits_map != map_key and no_digits_map in cc_map:
        entry = cc_map[no_digits_map]
        return entry.get("oid"), entry.get("name", no_digits_map)

    # 匹配候选：完整 target + 去尾部数字版本（如 kangxianghua001 → kangxianghua）
    targets = {target}
    no_digits = re.sub(r"\d+$", "", target)
    if no_digits and no_digits != target:
        targets.add(no_digits)

    candidates = []
    for m in members:
        oid = m.get("openDingtalkId") or m.get("openDingTalkId")
        if not oid:
            continue
        name = m.get("name", "")
        nick = m.get("nick", "")
        display = nick or name
        # 括号内容（昵称里的英文名/拼音）
        paren_m = re.search(r"[（(]([^)）]+)[)）]", nick)
        paren = paren_m.group(1) if paren_m else ""
        # 按空格切词
        words = [w for w in re.split(r"[\s_\-]+", nick) if len(w) >= 4]
        candidates.append({
            "oid": oid, "name": name, "nick": nick,
            "display": display, "paren": paren, "words": words,
        })

    # 1. 精确匹配：昵称整体 / 括号内容 / 真名归一化
    for c in candidates:
        for field in [c["nick"], c["paren"], c["name"]]:
            if norm(field) in targets:
                return c["oid"], c["display"]

    # 2. 切词精确匹配（短英文名如 THCC-pang → Pang Jennisa）
    word_hits = []
    for c in candidates:
        for w in c["words"]:
            if norm(w) in targets:
                word_hits.append(c)
    if len(word_hits) == 1:
        return word_hits[0]["oid"], word_hits[0]["display"]

    # 3. 拼音匹配（中文真名 → 全拼）
    try:
        from pypinyin import lazy_pinyin
        for c in candidates:
            if not c["name"]:
                continue
            py = "".join(lazy_pinyin(c["name"]))
            if norm(py) in targets:
                return c["oid"], c["display"]
    except ImportError:
        pass

    # 4. 模糊包含（长度 ≥ 6，避免短名误伤）
    min_target = min(targets, key=len)
    if len(min_target) >= 6:
        fuzzy_hits = []
        for c in candidates:
            nn = norm(c["nick"])
            nm = norm(c["name"])
            if any(t in nn or t in nm for t in targets):
                fuzzy_hits.append(c)
        if len(fuzzy_hits) == 1:
            return fuzzy_hits[0]["oid"], fuzzy_hits[0]["display"]

    return None


def fetch_members(group_cid):
    """拉群成员。失败或空列表抛异常（安全阀：不发文本@）。"""
    data = dws_json("chat", "+chat-members-list",
                    "--conversation-id", group_cid,
                    "--member-types", "user")
    users = data.get("users", [])
    if not users:
        raise RuntimeError("empty member list — abort to avoid text-only @")
    return users


def fetch_messages(group_cid, since_time, limit=100):
    data = dws_json("chat", "message", "list",
                    "--group", group_cid,
                    "--time", since_time,
                    "--direction", "newer",
                    "--limit", str(limit))
    return data.get("result", {}).get("messages", [])


def send_at_message(group_cid, user_id, cc_oid, msg_id, reply_tail="请跟进。"):
    """真 @ 发送：--at-open-dingtalk-ids + 正文 <@oid> 占位符，带幂等键。"""
    text = f"（{user_id}），<@{cc_oid}>  {reply_tail}"
    cmd = [
        "dws", "chat", "message", "send",
        "--group", group_cid,
        "--at-open-dingtalk-ids", cc_oid,
        "--uuid", f"atcc-{msg_id}",
        "--text", text,
        "--format", "json", "-y",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return False, r.stderr[:300]
    try:
        resp = json.loads(r.stdout)
        if resp.get("success"):
            return True, resp.get("result", {}).get("openTaskId", "")
        return False, resp.get("errorMsg", r.stdout[:200])
    except json.JSONDecodeError:
        return False, r.stdout[:200]


def send_dm_message(cc_oid, msg_id, original_content, reply_tail="please follow up"):
    """私信发送：把机器人原版消息内容 + 提醒语，单聊发给 CC。带幂等键。"""
    text = f"{original_content}\n\n{reply_tail}"
    cmd = [
        "dws", "chat", "message", "send",
        "--open-dingtalk-id", cc_oid,
        "--uuid", f"atcc-dm-{msg_id}",
        "--text", text,
        "--format", "json", "-y",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return False, r.stderr[:300]
    try:
        resp = json.loads(r.stdout)
        if resp.get("success"):
            return True, resp.get("result", {}).get("openTaskId", "")
        return False, resp.get("errorMsg", r.stdout[:200])
    except json.JSONDecodeError:
        return False, r.stdout[:200]


def process_group(group_cid, hours, dry_run, force, verbose, processed, reply_tail="请跟进。", dm=False):
    """处理单个群，返回该群的 results 列表。成员拉取失败返回 None（中止该群）。
    dm=True 时使用私信发送（把机器人原版内容+提醒语单聊发给CC），否则群内真@。"""
    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    today = datetime.now().strftime("%Y-%m-%d")
    mode = "DM" if dm else "AT"
    print(f"[INFO] group={group_cid} hours={hours} since={since} dry_run={dry_run} mode={mode}")

    try:
        members = fetch_members(group_cid)
    except Exception as e:
        print(f"[FATAL] {group_cid} fetch members failed: {e} — abort this group")
        return None
    print(f"[INFO] {group_cid} fetched {len(members)} members")

    # 私信模式：合并 CC 总群成员（CC 不一定在扫描群里，但一定在 CC 总群里）
    if dm:
        try:
            cc_members = fetch_members(DEFAULT_CC_GROUP)
            seen = {m.get("openDingtalkId") or m.get("openDingTalkId") for m in members}
            added = 0
            for m in cc_members:
                oid = m.get("openDingtalkId") or m.get("openDingTalkId")
                if oid and oid not in seen:
                    members.append(m)
                    seen.add(oid)
                    added += 1
            print(f"[INFO] merged CC group: +{added} members (total {len(members)})")
        except Exception as e:
            print(f"[WARN] fetch CC group members failed: {e} — using scan group members only")

    try:
        messages = fetch_messages(group_cid, since)
    except Exception as e:
        print(f"[FATAL] {group_cid} fetch messages failed: {e}")
        return None
    print(f"[INFO] {group_cid} fetched {len(messages)} messages")

    results = []
    for msg in messages:
        content = msg.get("content", "")
        msg_id = msg.get("openMessageId", "")
        create_time = msg.get("createTime", "")

        parsed = parse_bot_message(content)
        if not parsed:
            continue
        user_id, cc_account = parsed

        omid_key = f"{group_cid}|{msg_id}"
        biz_key = f"{group_cid}|biz|{user_id}|{cc_account}|{today}"
        if not force and (omid_key in processed or biz_key in processed):
            if verbose:
                print(f"[SKIP] {group_cid} already processed: user={user_id} cc={cc_account}")
            continue

        resolved = resolve_cc(cc_account, members)
        if not resolved:
            print(f"[WARN] {group_cid} unresolved CC: {cc_account} (user {user_id}) — skip")
            results.append({"group": group_cid, "user_id": user_id, "cc": cc_account, "status": "unresolved", "time": create_time})
            continue

        cc_oid, cc_name = resolved

        if dry_run:
            action = "DM" if dm else "@"
            print(f"[DRY] {group_cid} would {action} {cc_name} for user {user_id}")
            results.append({"group": group_cid, "user_id": user_id, "cc": cc_account, "cc_name": cc_name, "status": "dry", "time": create_time})
            continue

        if dm:
            ok, detail = send_dm_message(cc_oid, msg_id, content, reply_tail)
        else:
            ok, detail = send_at_message(group_cid, user_id, cc_oid, msg_id, reply_tail)
        if ok:
            processed[omid_key] = {"ts": datetime.now().isoformat(), "user_id": user_id, "cc": cc_account}
            processed[biz_key] = {"ts": datetime.now().isoformat(), "user_id": user_id, "cc": cc_account}
            save_json(PROCESSED_FILE, processed)
            action = "DM" if dm else "@"
            print(f"[OK] {group_cid} {action} {cc_name} for user {user_id}")
            results.append({"group": group_cid, "user_id": user_id, "cc": cc_account, "cc_name": cc_name, "status": "sent", "time": create_time})
        else:
            action = "DM" if dm else "send"
            print(f"[FAIL] {group_cid} {action} failed user={user_id} cc={cc_account}: {detail}")
            results.append({"group": group_cid, "user_id": user_id, "cc": cc_account, "status": "failed", "error": detail, "time": create_time})

    return results


def main():
    parser = argparse.ArgumentParser(description="AI外呼分配消息 → 真@CC提醒（支持多群）")
    parser.add_argument("--group", action="append", required=True,
                        help="群 openConversationId，可重复传。格式 cid 或 cid|回复语（如 cid|Please follow.）")
    parser.add_argument("--hours", type=float, default=0.2, help="回溯小时数（默认0.2=12分钟）")
    parser.add_argument("--dry-run", action="store_true", help="只解析不发送")
    parser.add_argument("--force", action="store_true", help="忽略去重记录重跑")
    parser.add_argument("--dm", action="store_true", help="私信模式：把机器人原版内容+提醒语单聊发给CC（仅泰国群使用）")
    parser.add_argument("-v", "--verbose", action="store_true", help="显示跳过的已处理消息")
    args = parser.parse_args()

    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print("[SKIP] another run in progress (lock exists)")
        return
    try:
        processed = load_json(PROCESSED_FILE, {})
        all_results = []

        for gspec in args.group:
            parts = gspec.split("|", 1)
            gcid = parts[0]
            reply_tail = parts[1] if len(parts) > 1 else "请跟进。"
            r = process_group(gcid, args.hours, args.dry_run, args.force, args.verbose, processed, reply_tail, dm=args.dm)
            if r is not None:
                all_results.extend(r)

        sent = sum(1 for r in all_results if r["status"] == "sent")
        unresolved = sum(1 for r in all_results if r["status"] == "unresolved")
        failed = sum(1 for r in all_results if r["status"] == "failed")
        print(f"[DONE] groups={len(args.group)} matched={len(all_results)} sent={sent} unresolved={unresolved} failed={failed}")

        save_json(LAST_RUN_FILE, {
            "ts": datetime.now().isoformat(),
            "groups": args.group,
            "hours": args.hours,
            "dm": args.dm,
            "matched": len(all_results),
            "sent": sent,
            "unresolved": unresolved,
            "failed": failed,
            "results": all_results,
        })
    finally:
        os.close(lock_fd)
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass


if __name__ == "__main__":
    main()
