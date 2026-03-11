from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional
import sqlite3, json, time, uuid, os
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
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
def list_subscribers():
    """Internal endpoint — subscriber count by package."""
    try:
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            "SELECT package, COUNT(*) as cnt FROM subscribers WHERE active=1 GROUP BY package"
        ).fetchall()
        conn.close()
        return {"subscribers": [{"package": r[0], "count": r[1]} for r in rows]}
    except Exception:
        return {"subscribers": []}
