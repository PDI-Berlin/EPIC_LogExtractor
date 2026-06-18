"""
NOMAD Upload Module (adapted from nomad_uploader_script/xrd_api.py)

Handles authentication, ZIP creation, and raw upload to a NOMAD Oasis.
Reusable by any script that needs to push folders to NOMAD.
"""

import os
import sys
import time
import zipfile
import yaml
import requests
from getpass import getpass

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yml")


# ── Config helpers ─────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
        except Exception:
            return {}
    return {}


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            yaml.dump(cfg, fh, default_flow_style=False)
    except Exception:
        pass


def yn_prompt(question, default="y"):
    suffix = " [Y/n]: " if default == "y" else " [y/N]: "
    while True:
        sys.stdout.write(question + suffix)
        sys.stdout.flush()
        choice = input().strip().lower()
        if choice == "":
            return default == "y"
        if choice in ("y", "yes"):
            return True
        if choice in ("n", "no"):
            return False


# ── Authentication (called once, reused for multiple uploads) ──────────────

def setup_auth():
    """
    Interactive auth flow: prompt for username, server URL, password.
    Returns (base_url, upload_id, token) or (None, None, None) on failure.
    """
    cfg = load_config()
    nomad_cfg = cfg.get("nomad", {})
    servers = nomad_cfg.get("servers", {})

    username = input("\n  NOMAD username: ").strip()
    if not username:
        print("  Username required — skipping upload.")
        return None, None, None

    # ── Base URL ────────────────────────────────────────────────────────
    user_srv = servers.get(username, {})
    base_url = user_srv.get("base_url")
    if base_url:
        print(f"  Server: {base_url}")
        if not yn_prompt("  Use this server?", "y"):
            base_url = input("  NOMAD Oasis URL: ").strip()
            servers.setdefault(username, {})["base_url"] = base_url
            save_config(cfg)
    else:
        base_url = input("  NOMAD Oasis URL: ").strip()
        servers.setdefault(username, {})["base_url"] = base_url
        save_config(cfg)

    # ── Password ────────────────────────────────────────────────────────
    token_url = base_url.rstrip("/") + "/auth/token"
    print(f"\n  Authenticating at: {token_url}")
    token = None
    while True:
        password = getpass(f"  Password for {username}: ")
        try:
            r = requests.post(
                token_url,
                data={"grant_type": "password", "username": username, "password": password},
                timeout=10,
            )
            if r.status_code == 200:
                token = r.json().get("access_token")
                print("  Authentication successful.")
                break
            print(f"  Auth failed: {r.status_code}")
        except Exception as e:
            print(f"  Error: {e}")
        if not yn_prompt("  Retry?", "y"):
            return None, None, None

    # ── Upload ID ───────────────────────────────────────────────────────
    upload_id = user_srv.get("upload_id")
    while True:
        if upload_id:
            print(f"\n  Upload ID: {upload_id}")
            if yn_prompt("  Use this Upload ID?", "y"):
                if _validate_upload_id(base_url, upload_id, token):
                    break
                else:
                    upload_id = None
                    continue
            else:
                upload_id = None
                continue

        upload_id = input("\n  Upload ID: ").strip()
        if not upload_id:
            print("  Upload ID required.")
            continue
        if _validate_upload_id(base_url, upload_id, token):
            servers.setdefault(username, {})["upload_id"] = upload_id
            save_config(cfg)
            break
        else:
            upload_id = None

    return base_url, upload_id, token


# ── ZIP ────────────────────────────────────────────────────────────────────

def create_zip(source_folder):
    """
    Create a ZIP archive of source_folder (preserving its internal structure).
    The ZIP is placed next to source_folder and named <folder_name>.zip.
    Returns the path to the ZIP file.
    """
    folder_name = source_folder.name
    zip_path = source_folder.parent / f"{folder_name}.zip"

    if zip_path.exists():
        zip_path.unlink()

    print(f"\n  Creating ZIP: {zip_path.name}")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, _dirs, files in os.walk(source_folder):
            for file in files:
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, source_folder.parent)
                zipf.write(full_path, arcname=arcname)

    return zip_path


# ── API helpers ────────────────────────────────────────────────────────────

def _validate_upload_id(base_url, upload_id, token):
    """Check if an upload ID exists on the server. Returns True/False."""
    url = base_url.rstrip("/") + f"/uploads/{upload_id}"
    headers = {"Authorization": f"Bearer {token}"}

    sys.stdout.write(f"  Validating upload ID {upload_id}... ")
    sys.stdout.flush()

    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            print("valid")
            return True
        elif r.status_code == 404:
            print("not found")
            return False
        else:
            print(f"status {r.status_code} (assuming valid)")
            return True
    except Exception as e:
        print(f"check skipped: {e}")
        return True


def upload_zip_raw(base_url, upload_id, zip_path, token):
    """Upload a ZIP file to NOMAD as raw data. Returns the folder name on success."""
    zip_filename = os.path.basename(zip_path)
    folder_name = os.path.splitext(zip_filename)[0]

    url = (
        base_url.rstrip("/")
        + f"/uploads/{upload_id}/raw/{folder_name}"
        + "?overwrite_if_exists=true&auto_decompress=true"
    )

    print(f"  Uploading {zip_filename} -> folder '{folder_name}' ...")

    try:
        with open(zip_path, "rb") as fh:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
            }
            r = requests.put(url, headers=headers, data=fh, timeout=300)

        if r.status_code in (200, 201):
            return folder_name
        else:
            print(f"  Upload failed: {r.status_code} - {r.text[:200]}")
            return None
    except Exception as e:
        print(f"  Network error: {e}")
        return None


def monitor_processing(base_url, upload_id, token, folder_name):
    """Poll the upload status until processing finishes."""
    url = base_url.rstrip("/") + f"/uploads/{upload_id}"
    headers = {"Authorization": f"Bearer {token}"}
    print("\n  Monitoring processing...")

    while True:
        try:
            time.sleep(2)
            r = requests.get(url, headers=headers)
            if r.status_code != 200:
                continue

            data = r.json().get("data", {})
            if data.get("process_running"):
                sys.stdout.write(f"\r    Status: {data.get('process_status')}   ")
                sys.stdout.flush()
                continue

            print(f"\n\n  Finished: {data.get('process_status')}")

            ent_url = base_url.rstrip("/") + f"/uploads/{upload_id}/entries?page_size=100"
            r_ent = requests.get(ent_url, headers=headers)
            if r_ent.status_code == 200:
                entries = r_ent.json().get("data", [])
                current = [e for e in entries if e.get("mainfile", "").startswith(f"{folder_name}/")]
                if current:
                    print("  Upload successful.")
                else:
                    print("  No entries found for this upload yet.")
            break
        except KeyboardInterrupt:
            break
        except Exception:
            break


# ── Upload a single folder (uses pre-authenticated credentials) ────────────

def upload_folder(folder_path, base_url, upload_id, token):
    """
    ZIP and upload a single folder. Credentials must be obtained
    from setup_auth() first and passed in.
    """
    print(f"\n  --- {folder_path.name} ---")
    zip_path = create_zip(folder_path)
    if zip_path.exists():
        success = upload_zip_raw(base_url, upload_id, zip_path, token)
        if success:
            monitor_processing(base_url, upload_id, token, folder_path.name)
