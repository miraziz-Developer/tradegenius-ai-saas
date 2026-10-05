import subprocess
import json
import os

DOCKER_IMAGE_NAME = "mt5-wine-trader:latest"

def start_client_container(user_id, login, password, server, strategy_config, telegram_chat_id=None):
    """
    Spawns an isolated lightweight headless Docker container for the client.
    Resource limit: 256MB RAM and 0.5 CPU to allow hosting hundreds of clients per server.
    """
    container_name = f"trader_{user_id}_{login}"
    
    # Check if already running
    check_cmd = ["docker", "ps", "-q", "-f", f"name={container_name}"]
    existing = subprocess.run(check_cmd, capture_output=True, text=True)
    if existing.stdout.strip():
        return {"status": "already_running", "container_name": container_name}

    strategy_str = json.dumps(strategy_config)
    
    cmd = [
        "docker", "run", "-d",
        "--name", container_name,
        "--restart", "unless-stopped",
        "--memory", "300m",
        "--cpus", "0.5",
        "-e", f"MT5_LOGIN={login}",
        "-e", f"MT5_PASSWORD={password}",
        "-e", f"MT5_SERVER={server}",
        "-e", f"TELEGRAM_CHAT_ID={telegram_chat_id or user_id}",
        "-e", f"STRATEGY_JSON={strategy_str}",
        DOCKER_IMAGE_NAME
    ]
    
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        container_id = res.stdout.strip()[:12]
        return {
            "status": "success",
            "container_id": container_id,
            "container_name": container_name
        }
    except subprocess.CalledProcessError as e:
        return {"status": "error", "error": e.stderr}

def stop_client_container(user_id, login):
    container_name = f"trader_{user_id}_{login}"
    cmd = ["docker", "rm", "-f", container_name]
    try:
        subprocess.run(cmd, capture_output=True, text=True)
        return {"status": "stopped", "container_name": container_name}
    except Exception as e:
        return {"status": "error", "error": str(e)}

def get_client_container_status(user_id, login):
    container_name = f"trader_{user_id}_{login}"
    cmd = ["docker", "inspect", "-f", "{{.State.Status}}", container_name]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode == 0:
        return res.stdout.strip()
    return "not_found"
