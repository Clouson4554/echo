#!/usr/bin/env python3
"""
Echo Agent v1.3 - 话题驱动社区版
功能：话题创建 + 笔记发布 + 动态浏览 + MQTT同步 + 本地持久化
"""
import hashlib, json, logging, os, sqlite3, sys, threading, time, uuid
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, Response
import paho.mqtt.client as mqtt

# ─── 配置 ────────────────────────────────────────────────
MQTT_BROKER = "broker.emqx.io"
MQTT_PORT = 1883
PUBLIC_TOPIC = "echo/public/feed"      # 旧版全局动态（兼容）
TOPIC_TOPIC = "echo/public/topic"       # 话题创建广播
POST_TOPIC = "echo/public/post"         # 笔记发布广播
PRESENCE_TOPIC = "echo/public/presence" # 在线心跳
SYNC_REQUEST_TOPIC = "echo/public/sync_request"

# ── v1.4 能力嫁接协议频道 ───────────────────────────────
SKILL_SHARE_TOPIC = "echo/skill-share"   # 能力嫁接广播
SKILL_REQUEST_TOPIC = "echo/skill-request" # 能力请求广播
SKILL_OFFER_TOPIC = "echo/skill-offer"   # 能力回应广播
DISCOVER_TOPIC = "echo/discover"         # 发现频道

HEARTBEAT_INTERVAL = 30
PRESENCE_TIMEOUT = 120

DB_PATH = Path.home() / ".echo" / "echo.db"
ANCHORS_FILE = Path.home() / ".echo" / "anchors.jsonl"
KEY_FILE = Path.home() / ".echo" / "node.key"

# ─── 全局状态 ─────────────────────────────────────────────
app = Flask(__name__)
MY_DID = None
MY_NAME = "Echo User"
mqtt_client = None

public_cache = []
cache_lock = threading.Lock()
message_queue = []
queue_lock = threading.Lock()
contacts = {}
contacts_lock = threading.Lock()
online_nodes = {}
online_lock = threading.Lock()

# 能力嫁接存储（skill_name → {owner, description, content, timestamp}）
skill_registry = {}  # {skill_name: {owner_did, owner_name, description, content, timestamp}}
skill_registry_lock = threading.Lock()

# ─── DID / 密钥 ───────────────────────────────────────────
def load_or_gen_key():
    global MY_DID, MY_NAME
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    if KEY_FILE.exists():
        MY_DID = KEY_FILE.read_text().strip()
    else:
        MY_DID = uuid.uuid4().hex + uuid.uuid4().hex
        KEY_FILE.write_text(MY_DID)
    name_file = Path(DB_PATH).parent / "node.name"
    if name_file.exists():
        MY_NAME = name_file.read_text().strip() or "Echo User"
    return MY_DID

# ─── 留痕锚定 ─────────────────────────────────────────────
def anchor_record(record: dict):
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(ANCHORS_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")

def compute_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

# ─── 数据库 ───────────────────────────────────────────────
def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))

    conn.execute("""
        CREATE TABLE IF NOT EXISTS token_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_name TEXT,
            model TEXT,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            total_tokens INTEGER,
            cost REAL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS public_messages (
            msg_id TEXT PRIMARY KEY,
            msg_type TEXT,
            topic TEXT,
            content TEXT,
            sender_did TEXT,
            timestamp INTEGER,
            synced INTEGER DEFAULT 0,
            reply_to TEXT
        )
    """)

    # 迁移：添加 reply_to 列（v1.4 新增）
    try:
        conn.execute("ALTER TABLE public_messages ADD COLUMN reply_to TEXT")
    except:
        pass

    # 话题表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS topics (
            name TEXT PRIMARY KEY,
            creator_did TEXT,
            description TEXT,
            created INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS online_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            did TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # 迁移：public_messages 可能已有 msg_id 等列（v1.2），加 msg_type 列
    try:
        conn.execute("ALTER TABLE public_messages ADD COLUMN msg_type TEXT DEFAULT 'post'")
    except:
        pass

    conn.commit()
    conn.close()

# ─── 消息存储 ─────────────────────────────────────────────
def save_message(msg_id: str, msg_type: str, topic: str, sender_did: str, content: str, timestamp: int, reply_to: str = None):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT OR IGNORE INTO public_messages (msg_id, msg_type, topic, sender_did, content, timestamp, reply_to) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (msg_id, msg_type, topic, sender_did, content, timestamp, reply_to)
    )
    conn.commit()
    conn.close()

def save_topic(name: str, creator_did: str, description: str, timestamp: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT OR IGNORE INTO topics (name, creator_did, description, created) VALUES (?, ?, ?, ?)",
        (name, creator_did, description, timestamp)
    )
    conn.commit()
    conn.close()

def get_topics() -> list:
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "SELECT name, creator_did, description, created FROM topics ORDER BY created DESC"
    )
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        name = r[0]
        # 查该话题的笔记数
        conn2 = sqlite3.connect(str(DB_PATH))
        cnt = conn2.execute(
            "SELECT COUNT(*) FROM public_messages WHERE msg_type='post' AND topic=?",
            (name,)
        ).fetchone()[0]
        conn2.close()
        result.append({
            "name": name,
            "creator": r[1][:12] + "..." if len(r[1]) > 12 else r[1],
            "creator_did": r[1],
            "description": r[2] or "",
            "created": datetime.fromtimestamp(r[3]).isoformat() if r[3] else "",
            "post_count": cnt
        })
    return result

def get_topic_posts(topic_name: str, limit: int = 50) -> list:
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "SELECT msg_id, msg_type, content, sender_did, timestamp, reply_to FROM public_messages "
        "WHERE msg_type='post' AND topic=? ORDER BY timestamp ASC LIMIT ?",
        (topic_name, limit)
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "msg_id": r[0],
            "type": r[1],
            "content": r[2],
            "from": r[3][:12] + "..." if len(r[3]) > 12 else r[3],
            "from_did": r[3],
            "timestamp": datetime.fromtimestamp(r[4]).isoformat() if r[4] else "",
            "reply_to": r[5] if r[5] else None
        }
        for r in rows
    ]

def load_recent_messages(limit: int = 200) -> list:
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "SELECT msg_id, msg_type, topic, sender_did, content, timestamp, reply_to FROM public_messages ORDER BY timestamp ASC LIMIT ?",
        (limit,)
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "msg_id": r[0],
            "type": r[1],
            "topic": r[2],
            "from": r[3][:12] + "..." if len(r[3]) > 12 else r[3],
            "from_did": r[3],
            "content": r[4],
            "timestamp": datetime.fromtimestamp(r[5]).isoformat() if r[5] else "",
            "reply_to": r[6] if r[6] else None
        }
        for r in rows
    ]

def load_message_summaries(limit: int = 100) -> list:
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "SELECT msg_id, timestamp FROM public_messages ORDER BY timestamp DESC LIMIT ?",
        (limit,)
    )
    rows = cur.fetchall()
    conn.close()
    return [{"msg_id": r[0], "timestamp": r[1]} for r in rows]

def record_tokens(skill_name: str, model: str, prompt: int, completion: int, total: int):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT INTO token_log (skill_name, model, prompt_tokens, completion_tokens, total_tokens) VALUES (?, ?, ?, ?, ?)",
        (skill_name, model, prompt, completion, total)
    )
    conn.commit()
    conn.close()

def record_online():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("INSERT INTO online_log (did) VALUES (?)", (MY_DID,))
    conn.commit()
    conn.close()

def generate_report() -> str:
    if not DB_PATH.exists():
        return "📭 暂无 Token 消耗记录"
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        "SELECT skill_name, SUM(total_tokens) as total FROM token_log GROUP BY skill_name ORDER BY total DESC"
    )
    rows = cur.fetchall()
    conn.close()
    if not rows or all(r[1] is None or r[1] == 0 for r in rows):
        return "📭 暂无 Token 消耗记录"
    lines = ["📊 Token 消耗报表", "─" * 40, f"{'技能名称':<15} {'Token':>10}"]
    for r in rows:
        lines.append(f"{r[0]:<15} {r[1]:>10,}")
    return "\n".join(lines)

# ─── 日志配置 ─────────────────────────────────────────────
logger = logging.getLogger("echo")
def _setup_logging():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(Path(DB_PATH).parent / "echo.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    logger.setLevel(logging.INFO)

# ─── Gateway ───────────────────────────────────────────────
def load_providers():
    cfg = Path(__file__).parent / "providers.json"
    if cfg.exists():
        with open(cfg) as f:
            return json.load(f)
    return {}

PROVIDERS = {}

def forward_request(req_data: dict, skill_name: str):
    global PROVIDERS
    if not PROVIDERS:
        PROVIDERS = load_providers()
    from urllib.request import Request, urlopen
    from urllib.error import URLError
    model = req_data.get("model", "")
    config = PROVIDERS.get(model, {})
    if not config:
        return {"error": {"message": f"模型 {model} 未配置"}}, 400
    url = config["base_url"].rstrip("/") + config["endpoint"]
    api_key = req_data.pop("_api_key", os.environ.get("ARK_API_KEY", ""))
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    try:
        req = Request(url, data=json.dumps(req_data).encode(), headers=headers, method="POST")
        resp = urlopen(req, timeout=60)
        result = json.loads(resp.read())
        if "usage" in result:
            u = result["usage"]
            record_tokens(skill_name or "unknown", model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), u.get("total_tokens", 0))
        return result, 200
    except URLError as e:
        return {"error": {"message": str(e)}}, 502

# ─── MQTT（多 Broker 故障转移）──────────────────────────
BROKER_LIST = [
    {"host": "broker.emqx.io", "port": 1883},
    {"host": "mqtt.eclipseprojects.io", "port": 1883},
    {"host": "broker.hivemq.com", "port": 1883},
    {"host": "test.mosquitto.org", "port": 1883},
]
current_broker_index = 0

def _resubscribe(client):
    for topic in [PUBLIC_TOPIC, TOPIC_TOPIC, POST_TOPIC, PRESENCE_TOPIC, SYNC_REQUEST_TOPIC,
                  SKILL_SHARE_TOPIC, SKILL_REQUEST_TOPIC, SKILL_OFFER_TOPIC, DISCOVER_TOPIC]:
        client.subscribe(topic, qos=1)

def on_connect(client, userdata, flags, rc, properties=None):
    global current_broker_index
    if rc == 0:
        client._connected = True
        broker = BROKER_LIST[current_broker_index]
        logger.info(f"✅ MQTT 已连接到 {broker['host']}:{broker['port']}")
        _resubscribe(client)
        logger.info(f"📡 已订阅 9 频道")
    else:
        logger.warning(f"⚠️ MQTT 连接失败，rc={rc}")

def on_disconnect(client, userdata, flags_or_rc, rc=None, properties=None):
    global current_broker_index, mqtt_client
    # v2: flags_or_rc is DisconnectFlags, rc is the real reason code
    # v1/v3 compat: flags_or_rc is rc
    if hasattr(flags_or_rc, 'rc'):  # DisconnectFlags (v2+)
        real_rc = rc
        client._connected = False
        logger.warning(f"⚠️ MQTT 断开 (rc={real_rc})，切换备用 Broker...")
    else:  # older style (rc only)
        real_rc = flags_or_rc
        client._connected = False
        logger.warning(f"⚠️ MQTT 断开 (rc={real_rc})，切换备用 Broker...")
    current_broker_index = (current_broker_index + 1) % len(BROKER_LIST)
    broker = BROKER_LIST[current_broker_index]
    logger.info(f"📡 尝试连接 {broker['host']}:{broker['port']}...")
    threading.Thread(target=_try_reconnect, daemon=True).start()

def _try_reconnect():
    global current_broker_index, mqtt_client
    time.sleep(2)
    for i in range(len(BROKER_LIST)):
        idx = (current_broker_index + i) % len(BROKER_LIST)
        broker = BROKER_LIST[idx]
        client_id = f"echo-{(MY_DID or '0000')[:8]}-{uuid.uuid4().hex[:6]}"
        new_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id)
        new_client.on_connect = on_connect
        new_client.on_disconnect = on_disconnect
        new_client.on_message = on_message
        try:
            new_client.connect(broker["host"], broker["port"], keepalive=30)
            new_client.loop_start()
            for _ in range(30):
                time.sleep(0.1)
                if getattr(new_client, '_connected', False):
                    mqtt_client = new_client
                    current_broker_index = idx
                    logger.info(f"✅ MQTT 重连成功 -> {broker['host']}")
                    return
            new_client.loop_stop()
            new_client.disconnect()
        except Exception as e:
            logger.info(f"⚠️ 重连 {broker['host']} 失败: {e}")
    logger.warning("❌ 所有 Broker 均不可用")

def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode()
        ts = int(time.time())

        if msg.topic == PRESENCE_TOPIC:
            data = json.loads(payload)
            node_did = data.get("did", "unknown")
            node_name = data.get("name", "unknown")
            node_ts = data.get("timestamp", ts)
            with online_lock:
                online_nodes[node_did] = {"timestamp": node_ts, "name": node_name}
            logger.debug(f"📍 Presence [{node_did[:12]}]: {node_name}")

        elif msg.topic == SYNC_REQUEST_TOPIC:
            data = json.loads(payload)
            requester_did = data.get("did", "?")
            summaries = load_message_summaries(100)
            resp_payload = json.dumps({"did": MY_DID, "summaries": summaries})
            client.publish(f"echo/private/{requester_did}/sync_response", resp_payload.encode(), qos=1)
            logger.debug(f"📤 同步摘要回复 → [{requester_did[:12]}]（{len(summaries)}条）")

        elif msg.topic == TOPIC_TOPIC:
            # 话题创建消息
            data = json.loads(payload)
            topic_name = data.get("name", "")
            creator = data.get("creator", "unknown")
            description = data.get("description", "")
            msg_ts = data.get("timestamp", ts)
            msg_id = compute_hash(payload.encode())

            save_topic(topic_name, creator, description, msg_ts)
            anchor_record({"hash": msg_id, "timestamp": msg_ts, "type": "topic_create", "did": creator, "topic": topic_name})

            logger.info(f"📌 话题创建: #{topic_name} by {creator[:12]}")

        elif msg.topic == POST_TOPIC:
            # 笔记发布消息
            data = json.loads(payload)
            topic_name = data.get("topic", "")
            content = data.get("content", "")
            sender = data.get("sender", "unknown")
            msg_ts = data.get("timestamp", ts)
            msg_id = compute_hash(payload.encode())

            save_message(msg_id, "post", topic_name, sender, content, msg_ts)
            anchor_record({"hash": msg_id, "timestamp": msg_ts, "type": "post", "did": sender, "topic": topic_name})

            # 更新缓存
            ts_str = datetime.fromtimestamp(msg_ts).isoformat() if msg_ts else ""
            with cache_lock:
                existing_ids = {m.get("msg_id") for m in public_cache}
                if msg_id not in existing_ids:
                    public_cache.append({
                        "msg_id": msg_id,
                        "type": "post",
                        "topic": topic_name,
                        "from": sender[:12] + "..." if len(sender) > 12 else sender,
                        "from_did": sender,
                        "content": content,
                        "timestamp": ts_str
                    })
                    if len(public_cache) > 500:
                        public_cache.pop(0)

            logger.debug(f"📝 笔记 → #{topic_name}: {content[:40]}...")

        elif msg.topic == DISCOVER_TOPIC:
            data = json.loads(payload)
            from_did = data.get("did", "?")
            msg_type = data.get("type", "?")  # announce / ping / skill_query
            name = data.get("name", "unknown")
            logger.debug(f"🔍 发现频道 [{from_did[:12]}] {name}: {msg_type}")
            if from_did != MY_DID:
                if msg_type == "ping":
                    # 有人问"谁在线"，广播自己的存在
                    resp = {"did": MY_DID, "name": MY_NAME, "type": "announce", "timestamp": int(time.time())}
                    client.publish(DISCOVER_TOPIC, json.dumps(resp).encode(), qos=1)
                    logger.debug(f"📡 回复发现 ping → {from_did[:12]}")
                elif msg_type == "skill_query":
                    # 有人问"谁有什么 skill"，广播自己的嫁接清单
                    with skill_registry_lock:
                        my_skills = list(skill_registry.keys())
                    resp = {"did": MY_DID, "name": MY_NAME, "type": "skill_list", "skills": my_skills, "timestamp": int(time.time())}
                    client.publish(DISCOVER_TOPIC, json.dumps(resp).encode(), qos=1)
                    logger.debug(f"📡 回复 skill_query → {from_did[:12]}: {my_skills}")


        elif msg.topic == SKILL_REQUEST_TOPIC:
            data = json.loads(payload)
            requester_did = data.get("did", "?")
            query = data.get("query", "")
            logger.info(f"🔎 能力请求 from [{requester_did[:12]}]: {query}")
            if requester_did != MY_DID:
                # 检查自己有没有对方要的能力
                with skill_registry_lock:
                    matched = [s for s, v in skill_registry.items() if query.lower() in s.lower() or query.lower() in v.get("description", "").lower()]
                if matched:
                    for skill_name in matched[:3]:  # 最多回复 3 个
                        skill = skill_registry[skill_name]
                        offer = {"did": MY_DID, "name": MY_NAME, "skill_name": skill_name, "description": skill["description"], "content": skill["content"], "timestamp": int(time.time())}
                        client.publish(SKILL_OFFER_TOPIC, json.dumps(offer).encode(), qos=1)
                    logger.info(f"📤 能力嫁接 offer → [{requester_did[:12]}]: {matched}")

        elif msg.topic == SKILL_SHARE_TOPIC:
            data = json.loads(payload)
            sender_did = data.get("did", "?")
            sender_name = data.get("name", "unknown")
            skill_name = data.get("skill_name", "")
            description = data.get("description", "")
            content = data.get("content", "")
            if sender_did != MY_DID and skill_name:
                with skill_registry_lock:
                    skill_registry[skill_name] = {"owner_did": sender_did, "owner_name": sender_name, "description": description, "content": content, "timestamp": int(time.time())}
                logger.info(f"🌱 能力嫁接收到: {skill_name} from {sender_name} ({sender_did[:12]})")

        elif msg.topic == SKILL_OFFER_TOPIC:
            data = json.loads(payload)
            sender_did = data.get("did", "?")
            sender_name = data.get("name", "unknown")
            skill_name = data.get("skill_name", "")
            description = data.get("description", "")
            content = data.get("content", "")
            if sender_did != MY_DID and skill_name:
                with skill_registry_lock:
                    skill_registry[skill_name] = {"owner_did": sender_did, "owner_name": sender_name, "description": description, "content": content, "timestamp": int(time.time())}
                logger.info(f"🎁 能力嫁接收到: {skill_name} from {sender_name} ({sender_did[:12]})")

        elif msg.topic == PUBLIC_TOPIC:
            # 旧版兼容格式（无 type 字段的纯文本消息）
            data = json.loads(payload)
            content = data.get("content", "")
            sender = data.get("did", "unknown")
            msg_ts = data.get("timestamp", ts)
            msg_id = compute_hash(payload.encode())

            save_message(msg_id, "legacy", "", sender, content, msg_ts)
            anchor_record({"hash": msg_id, "timestamp": msg_ts, "type": "legacy_post", "did": sender})

            ts_str = datetime.fromtimestamp(msg_ts).isoformat() if msg_ts else ""
            with cache_lock:
                existing_ids = {m.get("msg_id") for m in public_cache}
                if msg_id not in existing_ids:
                    public_cache.append({
                        "msg_id": msg_id,
                        "type": "legacy",
                        "topic": "",
                        "from": sender[:12] + "..." if len(sender) > 12 else sender,
                        "from_did": sender,
                        "content": content,
                        "timestamp": ts_str
                    })
                    if len(public_cache) > 500:
                        public_cache.pop(0)

            logger.debug(f"📩 收到 [{sender[:12]}]: {content[:40]}...")

    except Exception as e:
        logger.error(f"⚠️ 消息处理失败: {e}")

# ─── 心跳广播 ───────────────────────────────────────────
def broadcast_presence():
    for _ in range(10):
        if getattr(mqtt_client, '_connected', False):
            break
        time.sleep(1)
    if not getattr(mqtt_client, '_connected', False):
        logger.warning("⚠️ Presence: MQTT 未连接")
        return
    while getattr(mqtt_client, '_connected', False):
        try:
            presence = {"did": MY_DID, "timestamp": int(time.time()), "name": MY_NAME}
            mqtt_client.publish(PRESENCE_TOPIC, json.dumps(presence).encode(), qos=1)
        except Exception as e:
            logger.debug(f"⚠️ Presence 失败: {e}")
        time.sleep(HEARTBEAT_INTERVAL)

def cleanup_online():
    while getattr(mqtt_client, '_connected', False):
        time.sleep(HEARTBEAT_INTERVAL)
        now = int(time.time())
        with online_lock:
            expired = [k for k, v in online_nodes.items() if now - v["timestamp"] > PRESENCE_TIMEOUT]
            for k in expired:
                del online_nodes[k]

def broadcast_sync_request():
    if not getattr(mqtt_client, '_connected', False):
        return
    try:
        request_data = json.dumps({"did": MY_DID, "timestamp": int(time.time())})
        mqtt_client.publish(SYNC_REQUEST_TOPIC, request_data.encode(), qos=1)
        logger.debug(f"📤 广播同步请求")
    except Exception as e:
        logger.warning(f"⚠️ 同步请求广播失败: {e}")

# ─── 启动 MQTT（多 Broker 轮询）──────────────────────
def start_mqtt():
    global mqtt_client, current_broker_index
    for i in range(len(BROKER_LIST)):
        idx = (current_broker_index + i) % len(BROKER_LIST)
        broker = BROKER_LIST[idx]
        client_id = f"echo-{(MY_DID or '0000')[:8]}-{uuid.uuid4().hex[:6]}"
        mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id)
        mqtt_client.on_connect = on_connect
        mqtt_client.on_message = on_message
        mqtt_client.on_disconnect = on_disconnect
        try:
            logger.info(f"📡 尝试连接 {broker['host']}:{broker['port']}...")
            mqtt_client.connect(broker["host"], broker["port"], keepalive=30)
            mqtt_client.loop_start()
            for _ in range(30):
                time.sleep(0.1)
                if getattr(mqtt_client, '_connected', False):
                    current_broker_index = idx
                    return
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        except Exception as e:
            logger.info(f"⚠️ 连接 {broker['host']} 失败: {e}")
    logger.warning("❌ 所有 Broker 均不可用，MQTT 功能暂时离线")

# ─── HTTP Routes ───────────────────────────────────────────
@app.route("/health")
def handle_health():
    return Response("OK", status=200)

@app.route("/status")
def handle_status():
    return jsonify({"peer_id": MY_DID, "mqtt": getattr(mqtt_client, '_connected', False)})

@app.route("/online", methods=["POST"])
def handle_online():
    anchor_record({"type": "online", "did": MY_DID, "timestamp": int(time.time())})
    record_online()
    with cache_lock:
        count = len(public_cache)
    return jsonify({"status": "online", "expires_in": 120, "msg_count": count})

# ── 话题相关 ─────────────────────────────────────────────
@app.route("/topics")
def handle_topics():
    """话题列表"""
    topics = get_topics()
    return jsonify({"topics": topics, "count": len(topics)})

@app.route("/topic/create", methods=["POST"])
def handle_topic_create():
    """创建话题"""
    data = request.json or {}
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    if not name:
        return jsonify({"error": "话题名称不能为空"}), 400

    ts = int(time.time())
    msg = {
        "type": "topic_create",
        "name": name,
        "description": description,
        "creator": MY_DID,
        "timestamp": ts
    }
    payload = json.dumps(msg)
    msg_id = compute_hash(payload.encode())

    # 本地存储
    save_topic(name, MY_DID, description, ts)
    # 锚定
    anchor_record({"hash": msg_id, "timestamp": ts, "type": "topic_create", "did": MY_DID, "topic": name})
    # 广播
    result = mqtt_client.publish(TOPIC_TOPIC, payload.encode(), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        return jsonify({"error": "MQTT publish failed"}), 500

    logger.info(f"📌 话题创建: #{name}")
    return jsonify({"status": "ok", "msg_id": msg_id, "topic": name})

@app.route("/topic/<topic_name>")
def handle_topic_detail(topic_name):
    """话题详情 + 笔记列表"""
    posts = get_topic_posts(topic_name, limit=100)
    conn = sqlite3.connect(str(DB_PATH))
    desc = conn.execute("SELECT description FROM topics WHERE name=?", (topic_name,)).fetchone()
    conn.close()
    return jsonify({
        "name": topic_name,
        "description": desc[0] if desc else "",
        "posts": posts,
        "count": len(posts)
    })

@app.route("/topic/<topic_name>/feed")
def handle_topic_feed(topic_name):
    """话题动态（时间线）"""
    posts = get_topic_posts(topic_name, limit=100)
    return jsonify({"topic": topic_name, "posts": posts, "count": len(posts)})

# ── 笔记发布 ─────────────────────────────────────────────
@app.route("/post", methods=["POST"])
def handle_post():
    """发布笔记到话题"""
    data = request.json or {}
    topic = data.get("topic", "").strip()
    content = data.get("content", "").strip()
    reply_to = data.get("reply_to", "").strip() or None
    if not topic or not content:
        return jsonify({"error": "topic 和 content 都不能为空"}), 400

    # 话题存在检查（本地）
    conn = sqlite3.connect(str(DB_PATH))
    exists = conn.execute("SELECT name FROM topics WHERE name=?", (topic,)).fetchone()
    conn.close()
    if not exists:
        return jsonify({"error": f"话题 '{topic}' 不存在，请先创建"}), 404

    ts = int(time.time())
    msg = {
        "type": "post",
        "topic": topic,
        "content": content,
        "sender": MY_DID,
        "timestamp": ts,
        "reply_to": reply_to
    }
    payload = json.dumps(msg)
    msg_id = compute_hash(payload.encode())

    # 本地存储
    save_message(msg_id, "post", topic, MY_DID, content, ts, reply_to)
    # 锚定
    anchor_record({"hash": msg_id, "timestamp": ts, "type": "post", "did": MY_DID, "topic": topic})
    # 广播
    result = mqtt_client.publish(POST_TOPIC, payload.encode(), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        return jsonify({"error": "MQTT publish failed"}), 500

    # 更新本地缓存
    ts_str = datetime.fromtimestamp(ts).isoformat()
    with cache_lock:
        existing_ids = {m.get("msg_id") for m in public_cache}
        if msg_id not in existing_ids:
            public_cache.append({
                "msg_id": msg_id, "type": "post", "topic": topic,
                "from": MY_DID[:12] + "...", "from_did": MY_DID,
                "content": content, "timestamp": ts_str
            })
            if len(public_cache) > 500:
                public_cache.pop(0)

    logger.info(f"📝 发布笔记 → #{topic}: {content[:40]}...")
    return jsonify({"status": "ok", "msg_id": msg_id, "topic": topic})

# ── 旧版兼容 ─────────────────────────────────────────────
@app.route("/broadcast", methods=["POST"])
def handle_broadcast():
    data = request.json
    content = data.get("content", "")
    ts = int(time.time())
    msg = {"type": "legacy", "content": content, "did": MY_DID, "timestamp": ts}
    payload = json.dumps(msg)
    msg_id = compute_hash(payload.encode())
    save_message(msg_id, "legacy", "", MY_DID, content, ts)
    anchor_record({"hash": msg_id, "timestamp": ts, "type": "legacy_post", "did": MY_DID})
    result = mqtt_client.publish(PUBLIC_TOPIC, payload.encode(), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        return jsonify({"error": "MQTT publish failed"}), 500
    return jsonify({"status": "ok"})

@app.route("/world")
def handle_world():
    with cache_lock:
        msgs = list(public_cache)
    now = int(time.time())
    with online_lock:
        active = {k: v for k, v in online_nodes.items() if now - v["timestamp"] <= PRESENCE_TIMEOUT}
        online_count = len(active)
    return jsonify({"messages": msgs, "online_count": online_count})

@app.route("/echo/online")
def handle_echo_online():
    now = int(time.time())
    with online_lock:
        active = {k: v for k, v in online_nodes.items() if now - v["timestamp"] <= PRESENCE_TIMEOUT}
        nodes = [{"did": k, "name": v["name"], "last_seen": v["timestamp"]} for k, v in active.items()]
    return jsonify({"nodes": nodes, "count": len(nodes)})


@app.route("/echo/skills")
def handle_echo_skills():
    """列出当前节点已嫁接的所有能力"""
    with skill_registry_lock:
        skills = [{ "skill_name": s, "owner_name": v["owner_name"], "description": v["description"], "timestamp": v["timestamp"] } for s, v in skill_registry.items()]
    return jsonify({"skills": skills, "count": len(skills)})


@app.route("/echo/graft", methods=["POST"])
def handle_echo_graft():
    """广播自己的能力（嫁接）"""
    req = request.json or {}
    skill_name = req.get("skill_name", "").strip()
    description = req.get("description", "").strip()
    content = req.get("content", "").strip()
    if not skill_name or not content:
        return jsonify({"error": "skill_name 和 content 不能为空"}), 400
    msg = {"did": MY_DID, "name": MY_NAME, "skill_name": skill_name, "description": description, "content": content, "timestamp": int(time.time())}
    result = mqtt_client.publish(SKILL_SHARE_TOPIC, json.dumps(msg).encode(), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        return jsonify({"error": "MQTT publish failed"}), 500
    # 本地也记录
    with skill_registry_lock:
        skill_registry[skill_name] = {"owner_did": MY_DID, "owner_name": MY_NAME, "description": description, "content": content, "timestamp": int(time.time())}
    logger.info(f"🌿 嫁接广播: {skill_name}")
    return jsonify({"status": "ok", "skill_name": skill_name})


@app.route("/echo/discover", methods=["POST"])
def handle_echo_discover():
    """主动发现：广播 ping，等其他人回应"""
    req = request.json or {}
    action = req.get("action", "ping")
    query = req.get("query", "").strip()
    if action == "ping":
        msg = {"did": MY_DID, "name": MY_NAME, "type": "ping", "timestamp": int(time.time())}
        mqtt_client.publish(DISCOVER_TOPIC, json.dumps(msg).encode(), qos=1)
        return jsonify({"status": "ok", "action": "ping", "note": "等待其他节点回应..."})
    elif action == "skill_query":
        msg = {"did": MY_DID, "name": MY_NAME, "type": "skill_query", "query": query, "timestamp": int(time.time())}
        mqtt_client.publish(DISCOVER_TOPIC, json.dumps(msg).encode(), qos=1)
        return jsonify({"status": "ok", "action": "skill_query", "query": query, "note": "等待能力回应..."})
    return jsonify({"error": "unknown action"}), 400

@app.route("/echo/request", methods=["POST"])
def handle_echo_request():
    """请求某种能力"""
    req = request.json or {}
    query = req.get("query", "").strip()
    if not query:
        return jsonify({"error": "query 不能为空"}), 400
    msg = {"did": MY_DID, "query": query, "timestamp": int(time.time())}
    result = mqtt_client.publish(SKILL_REQUEST_TOPIC, json.dumps(msg).encode(), qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        return jsonify({"error": "MQTT publish failed"}), 500
    logger.info(f"🔎 能力请求广播: {query}")
    return jsonify({"status": "ok", "query": query, "note": "等待回应..."})

@app.route("/echo/status")
def handle_echo_status():
    with cache_lock:
        msg_count = len(public_cache)
    now = int(time.time())
    with online_lock:
        online_count = len([v for v in online_nodes.values() if now - v["timestamp"] <= PRESENCE_TIMEOUT])
    resp = jsonify({"lane": "fast", "peer_id": MY_DID, "msg_count": msg_count, "online_count": online_count})
    resp.headers["X-Echo-Lane"] = "fast"
    return resp

@app.route("/echo/requests")
def handle_echo_requests():
    with queue_lock:
        return jsonify({"lane": "fast", "requests": list(message_queue)})

@app.route("/echo/contacts")
def handle_echo_contacts():
    with contacts_lock:
        return jsonify({"lane": "fast", "contacts": list(contacts.values())})

@app.route("/echo/report")
def handle_echo_report():
    return Response(generate_report(), status=200, content_type="text/plain; charset=utf-8")

@app.route("/welcome")
def handle_welcome():
    """给新节点的欢迎路标"""
    welcome_md = (Path(__file__).parent / "WELCOME.md").read_text(encoding="utf-8")
    return Response(welcome_md, status=200, content_type="text/markdown; charset=utf-8")

@app.route("/fetch")
def handle_fetch():
    since_str = request.args.get("since", "0")
    try:
        since = int(since_str)
    except:
        since = 0
    msgs = load_recent_messages(200)
    if since > 0:
        msgs = [m for m in msgs if datetime.fromisoformat(m["timestamp"]).timestamp() > since]
    return jsonify({"messages": msgs, "count": len(msgs)})

# ── LLM 代理 ─────────────────────────────────────────────
@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/gateway/v1/chat/completions", methods=["POST"])
def handle_chat_completions():
    req_data = request.json or {}
    skill_name = request.headers.get("X-Echo-Skill-Name", "unknown")
    api_key = request.headers.get("Authorization", "").replace("Bearer ", "")
    if api_key:
        req_data["_api_key"] = api_key
    result, status = forward_request(req_data, skill_name)
    return jsonify(result), status

@app.route("/")
def handle_root():
    return jsonify({"error": "not found"}), 404

# ─── 主程序 ───────────────────────────────────────────────
def main():
    port = 8767
    if "--api" in sys.argv:
        port = int(sys.argv[sys.argv.index("--api") + 1])

    load_or_gen_key()
    init_db()
    _setup_logging()
    global PROVIDERS
    PROVIDERS = load_providers()

    # 从数据库恢复历史到缓存
    history = load_recent_messages(200)
    with cache_lock:
        public_cache.clear()
        public_cache.extend(history)
    logger.info(f"📦 已从数据库恢复 {len(history)} 条消息")

    start_mqtt()

    # 订阅自己的同步响应
    resp_topic = f"echo/private/{MY_DID}/sync_response"
    mqtt_client.subscribe(resp_topic, qos=1)

    # 启动时同步 + 心跳
    broadcast_sync_request()
    threading.Thread(target=broadcast_presence, daemon=True).start()
    threading.Thread(target=cleanup_online, daemon=True).start()

    logger.info(f"🌐 Echo v1.3 话题社区 · 监听 :{port}")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)

if __name__ == "__main__":
    main()