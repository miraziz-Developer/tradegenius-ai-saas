# TradeGenius AI: No-Code Multi-Tenant MT5 Algorithmic Trading SaaS

TradeGenius AI is a complete, scalable, and cost-effective SaaS platform that converts natural language trading strategies into automated 24/7 trading bots on MetaTrader 4 / 5 using a hybrid delivery model.

---

## 🏗️ Architecture Overview

```
                                [Customer Interface]
                             (Telegram Bot / Web App)
                                        │
                                        ▼
                           [Strategy Extraction Engine]
                         (Natural Language ──► JSON Schema)
                                        │
            ┌───────────────────────────┴───────────────────────────┐
            ▼                                                       ▼
   [Plan A: Self-Hosted ($19/mo)]                         [Plan B: Cloud Autopilot ($39/mo)]
   • Customer runs lightweight EA                        • Master Backend spawns isolated
     on their own MT5 terminal                             Docker Container (Wine + MT5 + Python)
   • Server cost: $0.00                                   • Container footprint: ~250MB RAM
                                                          • Infrastructure cost: ~$0.15/client/mo
```

---

## 📁 Repository Structure

* `Dockerfile`: Container image packaging Ubuntu 22.04 + Wine 64 + Headless Xvfb + Windows Python 3.11 + MetaTrader 5.
* `entrypoint.sh`: Container startup script that dynamically renders `account.ini`, launches MT5 headlessly, and triggers the Python engine.
* `strategy_engine.py`: Universal, dynamic rule engine executing inside the container; evaluates indicators (EMA, RSI, ATR, Swing Pivots) and executes risk-managed orders.
* `docker_manager.py`: Python daemon service orchestrating client containers (creation, monitoring, termination).
* `telegram_bot.py`: The user-facing conversational onboarding bot collecting strategies and provisioning instances.

---

## 🚀 Deployment Instructions (Ubuntu / Debian Server)

### 1. Prerequisites on Linux Host
```bash
sudo apt-get update
sudo apt-get install -y docker.io python3-pip
sudo systemctl enable --now docker
```

### 2. Build the Multi-Tenant Docker Image
```bash
cd saas_platform
docker build -t mt5-wine-trader:latest .
```

### 3. Start the Master Orchestrator Bot
```bash
pip3 install requests urllib3
python3 telegram_bot.py
```

---

## 💰 Unit Economics & Profitability

* **Cloud Server (Hetzner 64GB RAM Dedicated):** €45/month (~$50/mo)
* **Capacity per Server:** 250 - 350 active MT5 client containers
* **Gross Revenue (300 clients @ $39/mo):** **$11,700 / month**
* **Total Infrastructure Cost:** **$50 / month**
* **Net Margin:** **> 99%**
