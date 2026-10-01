#!/usr/bin/env python3
"""
Modal Multi-Account Cluster & API Key Rotation Manager
Manages deployment, health probing, failover, and adding new Modal accounts seamlessly.
"""

import os
import sys
import json
import argparse
import subprocess
import time
from typing import Dict, List, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CLUSTERS_FILE = os.path.join(SCRIPT_DIR, "modal_clusters.json")
MODAL_APP_PATH = os.path.join(SCRIPT_DIR, "modal_app.py")


def load_registry() -> dict:
    if not os.path.exists(CLUSTERS_FILE):
        return {"active_primary": "cmpunktg", "clusters": []}
    with open(CLUSTERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_registry(data: dict):
    with open(CLUSTERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_cluster(profile: str) -> Optional[dict]:
    reg = load_registry()
    for c in reg.get("clusters", []):
        if c.get("profile") == profile or c.get("id") == profile:
            return c
    return None


def cmd_status(args):
    import requests
    reg = load_registry()
    clusters = reg.get("clusters", [])
    print("\n" + "=" * 70)
    print(" 🚀 BANGAI STOCK STUDIO - MODAL CLUSTERS & ROTATION STATUS")
    print("=" * 70)

    for c in clusters:
        profile = c.get("profile")
        role = c.get("role", "secondary").upper()
        prio = c.get("priority", 99)
        serve_url = c.get("serve_url", "")
        ui_url = c.get("ui_url", "")
        credit = c.get("credit_estimate", "Unknown")

        status_serve = "UNKNOWN"
        latency_serve = 0.0
        try:
            t0 = time.time()
            res = requests.get(f"{serve_url}/ping", timeout=8)
            latency_serve = round((time.time() - t0) * 1000, 1)
            status_serve = f"ONLINE ({res.status_code}) [{latency_serve}ms]" if res.ok else f"ERROR ({res.status_code})"
        except Exception as e:
            status_serve = f"OFFLINE ({type(e).__name__})"

        print(f"\n▶ [{role} - Priority {prio}] Account Profile: {profile}")
        print(f"  Name:       {c.get('name')}")
        print(f"  Credit:     {credit}")
        print(f"  API Serve:  {serve_url} => {status_serve}")
        print(f"  Studio UI:  {ui_url}")

    print("\n" + "=" * 70 + "\n")


def cmd_deploy(args):
    reg = load_registry()
    target_profile = args.profile

    if args.all:
        profiles_to_deploy = [c.get("profile") for c in reg.get("clusters", []) if c.get("enabled", True)]
    elif target_profile:
        profiles_to_deploy = [target_profile]
    else:
        profiles_to_deploy = [reg.get("active_primary", "cmpunktg")]

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for prof in profiles_to_deploy:
        print(f"\n🚀 Deploying bangai-stock-studio to Modal profile: [{prof}] ...")
        cmd = [sys.executable, "-m", "modal", "deploy", "modal_app.py", f"--profile={prof}"]
        res = subprocess.run(cmd, cwd=SCRIPT_DIR, env=env)
        if res.returncode == 0:
            print(f"✅ Successfully deployed to profile [{prof}]!")
        else:
            print(f"❌ Failed to deploy to profile [{prof}] (Exit code {res.returncode})")


def cmd_add(args):
    reg = load_registry()
    profile = args.profile
    token_id = args.token_id
    token_secret = args.token_secret
    name = args.name or f"Modal Account ({profile})"
    credit = args.credit or "$1.00"
    priority = int(args.priority) if args.priority else len(reg.get("clusters", [])) + 1
    role = "backup" if priority > 1 else "primary"

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    print(f"\n🔑 Setting Modal CLI credentials for profile: [{profile}] ...")
    cmd = [
        sys.executable, "-m", "modal", "token", "set",
        f"--token-id={token_id}",
        f"--token-secret={token_secret}",
        f"--profile={profile}"
    ]
    res = subprocess.run(cmd, cwd=SCRIPT_DIR, env=env)
    if res.returncode != 0:
        print(f"❌ Failed to set token for {profile}")
        return

    # Update clusters file
    existing = False
    for c in reg.get("clusters", []):
        if c.get("profile") == profile:
            c["token_id"] = token_id
            c["token_secret"] = token_secret
            c["name"] = name
            c["priority"] = priority
            c["role"] = role
            c["credit_estimate"] = credit
            existing = True
            break

    if not existing:
        ui_url = f"https://{profile}--bangai-stock-studio-ui.modal.run"
        serve_url = f"https://{profile}--bangai-stock-studio-serve.modal.run"
        reg.setdefault("clusters", []).append({
            "id": profile,
            "name": name,
            "profile": profile,
            "token_id": token_id,
            "token_secret": token_secret,
            "ui_url": ui_url,
            "serve_url": serve_url,
            "role": role,
            "priority": priority,
            "credit_estimate": credit,
            "enabled": True
        })

    save_registry(reg)
    print(f"✅ Account [{profile}] successfully registered in modal_clusters.json!")
    print(f"  UI Endpoint:    https://{profile}--bangai-stock-studio-ui.modal.run")
    print(f"  Serve Endpoint: https://{profile}--bangai-stock-studio-serve.modal.run")
    print(f"\nTo deploy to this new cluster right away, run:")
    print(f"  python manage_clusters.py deploy --profile {profile}")


def cmd_test_render(args):
    profile = args.profile or "cmpunktg1"
    print(f"\n🎬 Running test video render on Modal profile: [{profile}] ...")
    test_code = """
import modal
import subprocess
import os

app = modal.App("bangai-render-test")
image = modal.Image.debian_slim().apt_install("ffmpeg", "fonts-deva")

@app.function(image=image, cpu=2, memory=2048, timeout=120)
def render_sample():
    out_path = "/root/test_sample.mp4"
    cmd = (
        'ffmpeg -y -f lavfi -i color=c=0x111827:s=720x1280:d=4 -vf '
        '"drawtext=text=\\'Bang AI Account Test\\':fontsize=36:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2" '
        '-c:v libx264 -pix_fmt yuv420p /root/test_sample.mp4'
    )
    subprocess.run(cmd, shell=True, check=True)
    size = os.path.getsize(out_path)
    return {"status": "ok", "size": size, "path": out_path}

@app.local_entrypoint()
def main():
    res = render_sample.remote()
    print("RENDER_SUCCESS:", res)
"""
    test_script = os.path.join(SCRIPT_DIR, "_tmp_test_render.py")
    with open(test_script, "w", encoding="utf-8") as f:
        f.write(test_code)

    try:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        cmd = [sys.executable, "-m", "modal", "run", f"--profile={profile}", test_script]
        res = subprocess.run(cmd, cwd=SCRIPT_DIR, env=env)
        if res.returncode == 0:
            print(f"\n✅ Test render passed on profile [{profile}]! The account is 100% active and working!")
        else:
            print(f"\n❌ Test render failed on profile [{profile}] (code {res.returncode})")
    finally:
        if os.path.exists(test_script):
            os.remove(test_script)


def main():
    parser = argparse.ArgumentParser(description="Modal Multi-Account Cluster & Rotation Manager")
    subparsers = parser.add_subparsers(dest="command", help="Sub-commands")

    # status
    p_status = subparsers.add_parser("status", help="Check status of all Modal clusters")

    # deploy
    p_deploy = subparsers.add_parser("deploy", help="Deploy BangAI app to clusters")
    p_deploy.add_argument("--profile", type=str, help="Deploy to a specific profile")
    p_deploy.add_argument("--all", action="store_true", help="Deploy to all registered profiles")

    # add
    p_add = subparsers.add_parser("add", help="Register a new Modal account / token")
    p_add.add_argument("--profile", type=str, required=True, help="Modal profile name (e.g. cmpunktg2)")
    p_add.add_argument("--token-id", type=str, required=True, help="Modal token ID (ak-...)")
    p_add.add_argument("--token-secret", type=str, required=True, help="Modal token secret (as-...)")
    p_add.add_argument("--name", type=str, help="Human readable name")
    p_add.add_argument("--priority", type=int, help="Priority (1 = primary, 2 = backup, etc.)")
    p_add.add_argument("--credit", type=str, help="Credit estimate (e.g. $1.00)")

    # test-render
    p_test = subparsers.add_parser("test-render", help="Run a test video render on a specific cluster")
    p_test.add_argument("--profile", type=str, default="cmpunktg1", help="Modal profile to test")

    args = parser.parse_args()
    if not args.command or args.command == "status":
        cmd_status(args)
    elif args.command == "deploy":
        cmd_deploy(args)
    elif args.command == "add":
        cmd_add(args)
    elif args.command == "test-render":
        cmd_test_render(args)


if __name__ == "__main__":
    main()
