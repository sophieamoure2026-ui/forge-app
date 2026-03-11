#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# deploy_neuforge_fleet.sh
# NeuForge Commando Fleet — One-Shot VPS Deploy Script
#
# Deploys all 6 NeuForge commando daemons to VPS-1:
#   - neuforge-harvester    (lead scraper)
#   - neuforge-outreach     (email sequences)
#   - neuforge-sms          (6-touch drip)
#   - neuforge-vgm          (voicemail drops)
#   - neuforge-callcenter   (BANT IVR, port 8002)
#   - neuforge-selector     (commando lifecycle governance)
#
# Usage:
#   chmod +x deploy_neuforge_fleet.sh
#   ./deploy_neuforge_fleet.sh
# ═══════════════════════════════════════════════════════════════

set -e

VPS_HOST="${VPS_HOST:-187.77.194.119}"
VPS_USER="${VPS_USER:-root}"
VPS_KEY="${VPS_KEY:-~/.ssh/id_rsa}"
REMOTE_DIR="/opt/titan"
LOG_DIR="/var/log/titan"

SSH="ssh -i $VPS_KEY -o StrictHostKeyChecking=no $VPS_USER@$VPS_HOST"
SCP="scp -i $VPS_KEY -o StrictHostKeyChecking=no"

# Files to deploy
DAEMONS=(
    "Titan_NeuForgeHarvester.py"
    "Titan_NeuForgeOutreach.py"
    "Titan_NeuForgeSMS.py"
    "Titan_NeuForgeVGM.py"
    "Titan_NeuForgeCallCenter.py"
    "Titan_CommandoSelector.py"
    "neuforge_injection.py"
)

SERVICES=(
    "neuforge-harvester"
    "neuforge-outreach"
    "neuforge-sms"
    "neuforge-vgm"
    "neuforge-callcenter"
    "neuforge-selector"
)

SERVICE_FILES=(
    "systemd/neuforge-harvester.service"
    "systemd/neuforge-outreach.service"
    "systemd/neuforge-sms.service"
    "systemd/neuforge-vgm.service"
    "systemd/neuforge-callcenter.service"
    "systemd/neuforge-selector.service"
)

echo "╔══════════════════════════════════════════════════════╗"
echo "║  NeuForge Commando Fleet — VPS-1 Deploy              ║"
echo "║  Target: $VPS_USER@$VPS_HOST                         ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── 1. ENSURE DIRECTORIES ────────────────────────────────────────
echo "▶ Creating remote directories..."
$SSH "mkdir -p $REMOTE_DIR $LOG_DIR"

# ── 2. COPY DAEMON FILES ──────────────────────────────────────────
echo "▶ Uploading daemon files..."
for f in "${DAEMONS[@]}"; do
    if [ -f "$f" ]; then
        echo "  → $f"
        $SCP "$f" "$VPS_USER@$VPS_HOST:$REMOTE_DIR/$f"
    else
        echo "  ⚠️  $f not found locally — skipping"
    fi
done

# ── 3. INSTALL DEPENDENCIES ───────────────────────────────────────
echo "▶ Installing Python dependencies on VPS..."
$SSH "cd $REMOTE_DIR && source venv/bin/activate 2>/dev/null || python3 -m venv venv && \
    venv/bin/pip install -q --upgrade pip && \
    venv/bin/pip install -q requests feedparser beautifulsoup4 fastapi uvicorn python-dotenv"

# ── 4. DEPLOY SYSTEMD SERVICES ───────────────────────────────────
echo "▶ Installing systemd service files..."
for f in "${SERVICE_FILES[@]}"; do
    service_name=$(basename "$f" .service)
    if [ -f "$f" ]; then
        echo "  → $service_name.service"
        $SCP "$f" "$VPS_USER@$VPS_HOST:/etc/systemd/system/${service_name}.service"
    else
        echo "  ⚠️  $f not found — skipping"
    fi
done

# ── 5. RELOAD & ENABLE ───────────────────────────────────────────
echo "▶ Reloading systemd and enabling services..."
$SSH "systemctl daemon-reload"

for svc in "${SERVICES[@]}"; do
    echo "  → enabling $svc"
    $SSH "systemctl enable $svc"
done

# ── 6. STOP OLD INSTANCES IF RUNNING ─────────────────────────────
echo "▶ Stopping any existing instances..."
for svc in "${SERVICES[@]}"; do
    $SSH "systemctl stop $svc 2>/dev/null || true"
done
sleep 2

# ── 7. START FLEET (in order) ────────────────────────────────────
echo "▶ Starting NeuForge Commando Fleet..."

# Selector first — governs the rest
$SSH "systemctl start neuforge-selector"
echo "  ✅ neuforge-selector (lifecycle governance — initial draft running)"
sleep 3

# Harvester next — feeds leads into Brevo
$SSH "systemctl start neuforge-harvester"
echo "  ✅ neuforge-harvester (AI lead scraper active)"
sleep 2

# Outreach
$SSH "systemctl start neuforge-outreach"
echo "  ✅ neuforge-outreach (email sequences armed)"
sleep 1

# SMS
$SSH "systemctl start neuforge-sms"
echo "  ✅ neuforge-sms (6-touch drip active)"
sleep 1

# VGM
$SSH "systemctl start neuforge-vgm"
echo "  ✅ neuforge-vgm (voicemail drops armed)"
sleep 1

# Call Center (FastAPI — goes last)
$SSH "systemctl start neuforge-callcenter"
echo "  ✅ neuforge-callcenter (IVR live on port 8002)"

# ── 8. STATUS CHECK ──────────────────────────────────────────────
echo ""
echo "▶ Fleet status check..."
echo ""
$SSH "for svc in ${SERVICES[*]}; do \
    status=\$(systemctl is-active \$svc 2>/dev/null); \
    if [ \"\$status\" = \"active\" ]; then \
        echo \"  ✅ \$svc — ONLINE\"; \
    else \
        echo \"  ❌ \$svc — \$status\"; \
    fi; \
done"

# ── 9. TAIL LOGS ─────────────────────────────────────────────────
echo ""
echo "▶ Recent log output (last 5 lines each):"
echo ""
for svc in "${SERVICES[@]}"; do
    log_name="${svc/neuforge-/neuforge_}"
    echo "── $svc ──"
    $SSH "tail -5 $LOG_DIR/${log_name}.log 2>/dev/null || echo '  (no log yet)'"
    echo ""
done

echo "╔══════════════════════════════════════════════════════╗"
echo "║  NeuForge Commando Fleet — DEPLOYED ✅               ║"
echo "║  Harvester · Outreach · SMS · VGM · IVR · Selector   ║"
echo "║  Flagship product: NeuForge.app                       ║"
echo "╚══════════════════════════════════════════════════════╝"
