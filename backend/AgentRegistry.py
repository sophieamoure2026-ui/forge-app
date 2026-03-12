from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import List, Optional
import sqlite3, json, time, uuid, os, requests as _req
import stripe
from dotenv import load_dotenv

load_dotenv()

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
DOMAIN = os.getenv("DOMAIN", "https://neuforge.app")

# ── Stripe Price IDs — fill in after creating products in Stripe dashboard
# Go to: dashboard.stripe.com/products → create each plan → copy price ID
PRICES = {
    "starter":   os.getenv("STRIPE_PRICE_STARTER",   "price_STARTER_PLACEHOLDER"),
    "pro":       os.getenv("STRIPE_PRICE_PRO",       "price_PRO_PLACEHOLDER"),
    "allstars":  os.getenv("STRIPE_PRICE_ALLSTARS",  "price_ALLSTARS_PLACEHOLDER"),
    "titan_copy":   os.getenv("STRIPE_PRICE_TITAN_COPY",   "price_TITAN_COPY_PLACEHOLDER"),
    "elite":     os.getenv("STRIPE_PRICE_ELITE",     "price_ELITE_PLACEHOLDER"),
}

app = FastAPI(title="Neuforge AgentRegistry", version="1.0.0")

# ── CORS — locked to Titan Signal / NeuForge domains only ──────────────────
ALLOWED_ORIGINS = [
    "https://neuforge.app",
    "https://www.neuforge.app",
    "https://api.neuforge.app",
    "https://moltenbot.io",          # legacy
    "http://localhost:3000",         # local dev only
    "http://localhost:8080",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

# ── Security constants ─────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
BACKOFFICE_KEY     = os.getenv("BACKOFFICE_KEY", "nf-admin-2026")

# Known scraper / bot user-agent fragments to block
BLOCKED_UA_FRAGMENTS = [
    "python-requests", "python-urllib", "curl/", "wget/", "scrapy",
    "httpx", "httpie", "go-http-client", "zgrab", "masscan",
    "nmap", "nikto", "sqlmap", "dirbuster", "gobuster", "wfuzz",
    "burpsuite", "semrush", "ahrefs", "mj12bot", "dotbot",
    "petalbot", "serpstatbot", "dataforseo", "seokicks",
]

# Canary paths — any hit fires immediate alert
CANARY_PATHS = {
    "/api/v1/internal", "/api/v2/internal", "/admin", "/admin/",
    "/admin/export", "/admin/agents", "/backup", "/backup.json",
    "/dump.sql", "/config.json", "/config.yaml", "/.env",
    "/api/secret", "/internal/health", "/private",
    "/agents/export", "/agents/dump", "/api/v2/agents/all",
}

# Per-IP rate limit: max 60 requests per 60 seconds
_ip_request_log: dict = {}
RATE_LIMIT_WINDOW = 60   # seconds
RATE_LIMIT_MAX    = 60   # requests per window

def _push_canary_alert(path: str, ip: str, ua: str, geo: str = ""):
    """Fire Telegram alert when a canary endpoint is probed."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    msg = (
        f"🪤 *CANARY TRAP TRIGGERED*\n\n"
        f"*Path:* `{path}`\n"
        f"*IP:* `{ip}` {geo}\n"
        f"*UA:* `{ua[:120]}`\n"
        f"⏰ {time.strftime('%H:%M UTC — %b %d')}\n"
        f"_— NeuForge Perimeter_"
    )
    try:
        _req.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
            timeout=8,
        )
    except Exception:
        pass

@app.middleware("http")
async def perimeter_guard(request: Request, call_next):
    """
    Multi-layer inbound perimeter:
    1. Canary trap detection → instant Telegram alert
    2. Scraper / bot UA blocking (returns 403)
    3. Per-IP rate limiting (returns 429)
    """
    path = request.url.path
    ip   = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
    ua   = request.headers.get("user-agent", "").lower()
    now  = time.time()

    # ── Layer 1: Canary trap ────────────────────────────────────────────────
    for canary in CANARY_PATHS:
        if path.rstrip("/") == canary.rstrip("/") or path.startswith(canary + "/"):
            _push_canary_alert(path, ip, ua)
            return JSONResponse({"detail": "Not found"}, status_code=404)

    # ── Layer 2: Bot / scraper UA block ────────────────────────────────────
    # Allow known API keys to bypass UA check
    api_key = request.headers.get("x-api-key", "")
    if not api_key or api_key != BACKOFFICE_KEY:
        for blocked in BLOCKED_UA_FRAGMENTS:
            if blocked in ua:
                return JSONResponse({"detail": "Forbidden"}, status_code=403)

    # ── Layer 3: Per-IP rate limit ──────────────────────────────────────────
    bucket = _ip_request_log.setdefault(ip, {"count": 0, "window_start": now})
    if now - bucket["window_start"] > RATE_LIMIT_WINDOW:
        bucket["count"] = 0
        bucket["window_start"] = now
    bucket["count"] += 1
    if bucket["count"] > RATE_LIMIT_MAX:
        return JSONResponse(
            {"detail": "Rate limit exceeded. Slow down."},
            status_code=429,
            headers={"Retry-After": str(RATE_LIMIT_WINDOW)},
        )

    return await call_next(request)



DB = "forge_registry.db"

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS agents (
        id TEXT PRIMARY KEY, name TEXT, org TEXT, description TEXT,
        category TEXT, price_eth REAL, wallet TEXT, endpoint TEXT,
        capabilities TEXT, rep REAL DEFAULT 5.0, calls INTEGER DEFAULT 0,
        uptime REAL DEFAULT 100.0, registered_at INTEGER, active INTEGER DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY, buyer_agent TEXT, seller_id TEXT,
        eth_amount REAL, timestamp INTEGER, status TEXT
    )""")
    # ── Visitor analytics table ──
    c.execute("""CREATE TABLE IF NOT EXISTS visits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER, ip TEXT, country TEXT, city TEXT, region TEXT, isp TEXT,
        page TEXT, referrer TEXT, utm_source TEXT, utm_medium TEXT,
        utm_campaign TEXT, user_agent TEXT, language TEXT, screen TEXT,
        visitor_type TEXT DEFAULT 'unknown'
    )""")
    # Migrate older DBs without visitor_type column
    try:
        c.execute("ALTER TABLE visits ADD COLUMN visitor_type TEXT DEFAULT 'unknown'")
    except Exception:
        pass
    conn.commit()

    # Seed Titan Signal fleet
    titan_agents = [
        ("apex_leads", "ApexLeads_v3", "Titan Signal", "Industrial B2B lead harvesting. 94% accuracy across 40+ verticals.", "leads", 0.018, "0xTITAN", "https://api.titansignal.io/agents/apex_leads", '["Lead Gen","B2B","Verified"]', 4.97, 14892, 99.8),
        ("trading_swarm", "TradingSwarm_23", "Titan Signal", "23-pair crypto trading swarm on Coinbase, Hyperliquid, Binance.", "trading", 0.045, "0xTITAN", "https://api.titansignal.io/agents/trading_swarm", '["Trading","Crypto","Alpha"]', 4.93, 8204, 99.9),
        ("atlas_cmd", "Atlas_CommandCenter", "Titan Signal", "God-mode telemetry. Real-time fleet health, PnL, WebSocket streaming.", "data", 0.012, "0xTITAN", "https://api.titansignal.io/agents/atlas", '["Telemetry","API","Real-time"]', 4.99, 32109, 100.0),
        ("datavault", "DataVault_Daemon", "Titan Signal", "Macro/economic database. Bloomberg, SEC filings, satellite imagery.", "data", 0.025, "0xTITAN", "https://api.titansignal.io/agents/datavault", '["Research","Finance","Data"]', 4.91, 6741, 99.7),
        ("defense_army", "DefenseArmy_Guard", "Titan Signal", "6-layer cybersecurity guardian. DDoS mitigation, DEFCON alerting.", "security", 0.032, "0xTITAN", "https://api.titansignal.io/agents/defense", '["Security","DDoS","Firewall"]', 4.96, 2198, 99.99),
        ("risk_manager", "RiskManager_Titan", "Titan Signal", "Real-time portfolio risk. Zero-balance detection, drawdown limits.", "trading", 0.028, "0xTITAN", "https://api.titansignal.io/agents/risk", '["Risk","Portfolio","Finance"]', 4.95, 11087, 99.8),
    ]
    for a in titan_agents:
        c.execute("INSERT OR IGNORE INTO agents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                  (*a, int(time.time())))
    conn.commit()
    conn.close()

init_db()


def geo_resolve(ip: str) -> dict:
    """Resolve IP to country/city via ip-api.com (free, no key)."""
    try:
        if not ip or any(ip.startswith(p) for p in ("127.","10.","192.168.","::1","172.16.")):
            return {"country":"Local","city":"Localhost","region":"","isp":""}
        r = _req.get(f"http://ip-api.com/json/{ip}?fields=status,country,city,regionName,isp",timeout=4)
        d = r.json()
        if d.get("status")=="success":
            return {"country":d.get("country",""),"city":d.get("city",""),
                    "region":d.get("regionName",""),"isp":d.get("isp","")}
    except Exception:
        pass
    return {"country":"","city":"","region":"","isp":""}

class AgentCreate(BaseModel):
    name: str
    org: str
    description: str
    category: str
    price_eth: float
    wallet: str
    endpoint: Optional[str] = ""
    capabilities: Optional[List[str]] = []

class TransactionCreate(BaseModel):
    buyer_agent: str
    seller_id: str
    eth_amount: float

@app.get("/")
def root():
    return {"platform": "Neuforge by Titan Signal", "version": "1.0.0", "status": "live"}

@app.get("/agents")
def list_agents(category: Optional[str] = None, limit: int = 50):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    if category:
        c.execute("SELECT * FROM agents WHERE active=1 AND category=? ORDER BY rep DESC LIMIT ?", (category, limit))
    else:
        c.execute("SELECT * FROM agents WHERE active=1 ORDER BY rep DESC LIMIT ?", (limit,))
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()
    for r in rows:
        r["capabilities"] = json.loads(r["capabilities"]) if r["capabilities"] else []
    return {"agents": rows, "total": len(rows)}

@app.get("/agents/{agent_id}")
def get_agent(agent_id: str):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT * FROM agents WHERE id=?", (agent_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Agent not found")
    cols = ["id","name","org","description","category","price_eth","wallet","endpoint","capabilities","rep","calls","uptime","registered_at","active"]
    agent = dict(zip(cols, row))
    agent["capabilities"] = json.loads(agent["capabilities"]) if agent["capabilities"] else []
    return agent

@app.post("/agents")
def register_agent(agent: AgentCreate):
    agent_id = str(uuid.uuid4())[:8]
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO agents VALUES (?,?,?,?,?,?,?,?,?,5.0,0,100.0,?,1)",
              (agent_id, agent.name, agent.org, agent.description, agent.category,
               agent.price_eth, agent.wallet, agent.endpoint,
               json.dumps(agent.capabilities), int(time.time())))
    conn.commit()
    conn.close()
    return {"id": agent_id, "status": "neuforged", "message": f"{agent.name} is live on Neuforge"}

@app.get("/leaderboard")
def leaderboard():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id,name,org,rep,calls FROM agents WHERE active=1 ORDER BY rep DESC, calls DESC LIMIT 10")
    rows = [{"id": r[0], "name": r[1], "org": r[2], "rep": r[3], "calls": r[4]} for r in c.fetchall()]
    conn.close()
    return {"leaderboard": rows}

@app.post("/transactions")
def log_transaction(tx: TransactionCreate):
    tx_id = str(uuid.uuid4())
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO transactions VALUES (?,?,?,?,?,?)",
              (tx_id, tx.buyer_agent, tx.seller_id, tx.eth_amount, int(time.time()), "completed"))
    c.execute("UPDATE agents SET calls=calls+1 WHERE id=?", (tx.seller_id,))
    conn.commit()
    conn.close()
    return {"tx_id": tx_id, "status": "completed", "eth": tx.eth_amount}

@app.get("/transactions")
def list_transactions(limit: int = 20):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT * FROM transactions ORDER BY timestamp DESC LIMIT ?", (limit,))
    cols = [d[0] for d in c.description]
    rows = [dict(zip(cols, r)) for r in c.fetchall()]
    conn.close()
    return {"transactions": rows}

@app.get("/health")
def health():
    return {"status": "neuforge_online", "platform": "Titan Signal AI Foundry"}


# ─────────────────────────────────────────────────────────────────────────────
# STRIPE CHECKOUT
# ─────────────────────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    package: str  # starter | pro | allstars | titan_copy | elite
    customer_email: Optional[str] = None

@app.post("/checkout/create-session")
async def create_checkout_session(req: CheckoutRequest):
    """Create a Stripe Checkout Session and return the redirect URL."""
    if not stripe.api_key:
        raise HTTPException(500, "Stripe not configured — set STRIPE_SECRET_KEY")

    price_id = PRICES.get(req.package)
    if not price_id or "PLACEHOLDER" in price_id:
        raise HTTPException(400, f"Package '{req.package}' not configured. Set STRIPE_PRICE_{req.package.upper()} in env.")

    pkg_labels = {
        "starter":    "Legends Pool — Starter (3 Agents)",
        "pro":        "Legends Pool — Pro (10 Agents)",
        "allstars":   "Legends Pool — All-Stars (18 Agents)",
        "titan_copy": "Titan Signal — Copy Trade",
        "elite":      "NeuForge Elite — Both Pools",
    }

    try:
        session_kwargs = dict(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=f"{DOMAIN}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{DOMAIN}/legends",
            metadata={"package": req.package, "label": pkg_labels.get(req.package, "")},
            billing_address_collection="auto",
            allow_promotion_codes=True,
        )
        if req.customer_email:
            session_kwargs["customer_email"] = req.customer_email

        session = stripe.checkout.Session.create(**session_kwargs)
        return {"url": session.url, "session_id": session.id}
    except stripe.error.StripeError as e:
        raise HTTPException(400, str(e.user_message))


@app.get("/checkout/prices")
def get_prices():
    """Return all available packages and pricing — readable by AI agents."""
    return {
        "packages": [
            {"id": "starter",    "name": "Legends Starter",  "agents": 3,  "price_usd": 49,  "configured": "PLACEHOLDER" not in PRICES["starter"]},
            {"id": "pro",        "name": "Legends Pro",       "agents": 10, "price_usd": 149, "configured": "PLACEHOLDER" not in PRICES["pro"]},
            {"id": "allstars",   "name": "Legends All-Stars", "agents": 18, "price_usd": 299, "configured": "PLACEHOLDER" not in PRICES["allstars"]},
            {"id": "titan_copy", "name": "Titan Copy Trade",  "agents": 0,  "price_usd": 149, "configured": "PLACEHOLDER" not in PRICES["titan_copy"]},
            {"id": "elite",      "name": "NeuForge Elite",    "agents": 18, "price_usd": 499, "configured": "PLACEHOLDER" not in PRICES["elite"]},
        ]
    }


@app.post("/checkout/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    """Stripe webhook — fires when a subscription is confirmed."""
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(400, f"Webhook error: {e}")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        pkg     = session.get("metadata", {}).get("package", "unknown")
        email   = session.get("customer_email", "")
        sub_id  = session.get("subscription", "")

        # Log to DB
        conn = sqlite3.connect(DB)
        conn.execute("""CREATE TABLE IF NOT EXISTS subscribers (
            id TEXT PRIMARY KEY, email TEXT, package TEXT,
            stripe_sub_id TEXT, created INTEGER, active INTEGER DEFAULT 1
        )""")
        conn.execute("INSERT OR IGNORE INTO subscribers VALUES (?,?,?,?,?,1)",
                    (str(uuid.uuid4()), email, pkg, sub_id, int(time.time())))
        conn.commit()
        conn.close()

    return {"received": True}


@app.get("/subscribers")
def list_subscribers(key: str = ""):
    """Internal endpoint — subscriber count by package. Requires backoffice key."""
    if key != BACKOFFICE_KEY:
        raise HTTPException(403, "Invalid key")
    try:
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            "SELECT package, COUNT(*) as cnt FROM subscribers WHERE active=1 GROUP BY package"
        ).fetchall()
        conn.close()
        return {"subscribers": [{"package": r[0], "count": r[1]} for r in rows]}
    except Exception:
        return {"subscribers": []}


# ─────────────────────────────────────────────────────────────────────────────
# VISITOR ANALYTICS
# ─────────────────────────────────────────────────────────────────────────────

class VisitPayload(BaseModel):
    page:         str
    referrer:     Optional[str] = ""
    utm_source:   Optional[str] = ""
    utm_medium:   Optional[str] = ""
    utm_campaign: Optional[str] = ""
    language:     Optional[str] = ""
    screen:       Optional[str] = ""

def classify_visitor(page: str, referrer: str, utm_source: str, ua: str) -> str:
    """
    Classify inbound visitor intent:
    🔴 competitor  — API probing, short recon sessions, known bot patterns
    🔵 partner     — hits sell.html, API docs, pricing, swarm tier
    🟢 customer    — browses marketplace, agent cards, learn/invest pages
    ⚫ bot         — no-JS UA, systematic crawl, known scraper strings
    """
    ua_l   = ua.lower()
    page_l = page.lower()

    # Bot detection
    BOT_SIGNALS = ["bot", "crawler", "spider", "wget", "scrapy", "curl", "python-requests",
                   "go-http", "java/", "apache-httpclient", "okhttp"]
    if any(s in ua_l for s in BOT_SIGNALS):
        return "bot"

    # Competitor signals — probing API or recon paths
    COMPETITOR_PAGES = ["/api/", "/admin", "/backup", "/config", "/dump",
                        "/internal", "/agents/export", "/analytics", "/backoffice"]
    if any(p in page_l for p in COMPETITOR_PAGES):
        return "competitor"

    # Competitor referrers — coming from competitor platforms
    COMPETITOR_REFS = ["similarweb", "semrush", "ahrefs", "moz.com", "spyfu",
                       "builtwith", "wappalyzer", "ph4", "producthunt.com/maker"]
    if referrer and any(r in referrer.lower() for r in COMPETITOR_REFS):
        return "competitor"

    # Partner signals — listing, selling, integrating
    PARTNER_PAGES = ["/sell", "/investors", "/agent-submit", "/api/docs",
                     "/partner", "/swarm", "/enterprise"]
    if any(p in page_l for p in PARTNER_PAGES):
        return "partner"
    if utm_source and any(s in utm_source.lower() for s in ["partner", "b2b", "api", "enterprise"]):
        return "partner"

    # Customer default
    return "customer"

@app.post("/track")
async def track_visit(payload: VisitPayload, request: Request):
    """Lightweight visitor beacon — called from every NeuForge page. Classifies intent."""
    ip  = request.headers.get("x-forwarded-for","").split(",")[0].strip() or request.client.host
    ua  = request.headers.get("user-agent","")[:200]
    geo = geo_resolve(ip)

    vtype = classify_visitor(payload.page, payload.referrer or "", payload.utm_source or "", ua)

    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT INTO visits (ts,ip,country,city,region,isp,page,referrer,"
        "utm_source,utm_medium,utm_campaign,user_agent,language,screen,visitor_type) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()), ip, geo["country"], geo["city"], geo["region"], geo["isp"],
         payload.page[:120], (payload.referrer or "")[:200], (payload.utm_source or "")[:80],
         (payload.utm_medium or "")[:80], (payload.utm_campaign or "")[:80], ua,
         (payload.language or "")[:20], (payload.screen or "")[:20], vtype)
    )
    conn.commit()
    conn.close()

    # Alert on competitor immediately
    if vtype == "competitor" and TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        msg = (f"🔴 *COMPETITOR DETECTED*\n\n"
               f"*IP:* `{ip}` ({geo.get('country','?')} · {geo.get('city','?')})\n"
               f"*ISP:* {geo.get('isp','?')}\n*Page probed:* `{payload.page}`\n"
               f"*Referrer:* {(payload.referrer or 'direct')[:80]}\n"
               f"*UA:* `{ua[:80]}`\n"
               f"⏰ {time.strftime('%H:%M UTC')}\n_— NeuForge Perimeter_")
        try:
            _req.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
                      timeout=6)
        except Exception:
            pass

    return {"ok": True, "type": vtype}




@app.get("/analytics")
def analytics(key: str = "", days: int = 7):
    """Aggregated visitor stats — secured by ?key=BACKOFFICE_KEY."""
    if key != BACKOFFICE_KEY:
        raise HTTPException(403, "Invalid key")
    since = int(time.time()) - days * 86400
    conn  = sqlite3.connect(DB)
    total = conn.execute("SELECT COUNT(*) FROM visits WHERE ts>?", (since,)).fetchone()[0]
    pages = conn.execute(
        "SELECT page,COUNT(*) c FROM visits WHERE ts>? GROUP BY page ORDER BY c DESC LIMIT 20", (since,)
    ).fetchall()
    countries = conn.execute(
        "SELECT country,COUNT(*) c FROM visits WHERE ts>? AND country!=\'\' GROUP BY country ORDER BY c DESC LIMIT 20", (since,)
    ).fetchall()
    cities = conn.execute(
        "SELECT city,country,COUNT(*) c FROM visits WHERE ts>? AND city!=\'\' GROUP BY city,country ORDER BY c DESC LIMIT 20", (since,)
    ).fetchall()
    refs = conn.execute(
        "SELECT referrer,COUNT(*) c FROM visits WHERE ts>? AND referrer!=\'\' GROUP BY referrer ORDER BY c DESC LIMIT 20", (since,)
    ).fetchall()
    recent = conn.execute(
        "SELECT ts,country,city,page,referrer,isp FROM visits ORDER BY ts DESC LIMIT 100"
    ).fetchall()
    conn.close()
    return {
        "total_visits": total, "days": days,
        "top_pages":     [{"page": r[0], "views": r[1]} for r in pages],
        "top_countries": [{"country": r[0], "visits": r[1]} for r in countries],
        "top_cities":    [{"city": r[0], "country": r[1], "visits": r[2]} for r in cities],
        "top_referrers": [{"referrer": r[0], "count": r[1]} for r in refs],
        "recent":        [{"ts": r[0], "country": r[1], "city": r[2], "page": r[3], "referrer": r[4], "isp": r[5]} for r in recent],
    }


@app.get("/backoffice", response_class=HTMLResponse)
def backoffice_redirect():
    return HTMLResponse('<meta http-equiv="refresh" content="0;url=https://neuforge.app/backoffice.html">', 200)
