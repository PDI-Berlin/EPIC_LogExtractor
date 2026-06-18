"""
XRD File Uploader to NOMAD via API

Automated workflow for processing XRDML files,
creating upload packages, and sending them to NOMAD.

Key Features:
- Binary XML patching (preserves structure)
- Upload ID validation after file preparation
- Flat ZIP structure for NOMAD compatibility
"""

import os
import sys
import re
import yaml
import shutil
import zipfile
import time
import requests
from datetime import datetime
from getpass import getpass

CONFIG_FILE = "config.yml"
DEFAULT_DATA_DIR = "data"

# -----------------------
# Config & Setup
# -----------------------
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
        except: return {}
    return {}

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            yaml.dump(cfg, fh)
    except: pass

def yn_prompt(question, default="y"):
    suffix = " [Y/n]: " if default == "y" else " [y/N]: "
    while True:
        sys.stdout.write(question + suffix)
        sys.stdout.flush()
        choice = input().strip().lower()
        if choice == "": return (default == "y")
        if choice in ("y", "yes"): return True
        if choice in ("n", "no"): return False

def timestamp_now():
    return datetime.now().strftime("%Y%m%d%H%M")

# -----------------------
# BINARY PATCHING (The Fix)
# -----------------------
def process_xrdml_binary(original_path, full_name, sample_id, username):
    """
    Reads file as raw bytes.
    - <id>: Overwrites with sample_id
    - <author><name>: Overwrites with full_name
    - <sample><name> & <preparedBy>: Untouched
    """
    filename = os.path.basename(original_path)
    
    try:
        with open(original_path, 'rb') as f:
            content = f.read()
        
        modified = False
        
        # Encode strings to UTF-8 bytes for replacement
        b_full_name = full_name.encode('utf-8')
        b_sample_id = sample_id.encode('utf-8')
        
        # 1. SAMPLE ID (<id>...</id>)
        pattern_id = re.compile(rb'(<id>)(.*?)(</id>)', re.DOTALL)
        if pattern_id.search(content):
            content, count = pattern_id.subn(rb'\1' + b_sample_id + rb'\3', content)
            if count > 0: modified = True

        # 2. AUTHOR NAME (<author><name>...</name>)
        pattern_author = re.compile(rb'(<author>\s*<name>)(.*?)(</name>)', re.DOTALL)
        if pattern_author.search(content):
            content, count = pattern_author.subn(rb'\1' + b_full_name + rb'\3', content)
            if count > 0: modified = True

        # Save Output
        folder = os.path.dirname(original_path)
        base_name = os.path.splitext(filename)[0]
        
        # Naming: [filename]_processed.xrdml
        new_filename = f"{base_name}_processed.xrdml"
        output_path = os.path.join(folder, new_filename)
        
        with open(output_path, 'wb') as f:
            f.write(content)
            
        return True, output_path
    except Exception as e:
        print(f"❌ Error processing {filename}: {e}")
        return False, None

# -----------------------
# Zip (Flat Structure)
# -----------------------
def create_flat_zip(source_folder, zip_base_name):
    """Creates a ZIP with files at the ROOT (no subfolders)."""
    zip_filename = f"{zip_base_name}.zip"
    zip_path = os.path.join(source_folder, zip_filename)
    
    if os.path.exists(zip_path):
        os.remove(zip_path)
        
    print(f"\n📦 Creating Flat ZIP: {zip_filename}")
    
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(source_folder):
            for file in files:
                if file == zip_filename: continue
                
                # Skip original .xrdml (only include processed)
                if file.lower().endswith('.xrdml') and '_processed' not in file.lower():
                    continue
                
                full_path = os.path.join(root, file)
                zipf.write(full_path, arcname=file)
                
    return zip_path

# -----------------------
# API & Upload
# -----------------------
def authenticate(base_url, username):
    token_url = base_url.rstrip('/') + '/auth/token'
    print(f"\n🔐 Authenticating at: {token_url}")
    while True:
        password = getpass(f"Password for {username}: ")
        try:
            r = requests.post(token_url, data={
                'grant_type': 'password', 'username': username, 'password': password
            }, timeout=10)
            if r.status_code == 200:
                print("✔ Authentication successful.")
                return r.json().get('access_token')
            print(f"❌ Auth failed: {r.status_code}")
        except Exception as e:
            print(f"❌ Error: {e}")
        if not yn_prompt("Retry?"): return None

def get_user_name(base_url, token):
    try:
        r = requests.get(base_url.rstrip('/') + '/users/me', headers={'Authorization': f'Bearer {token}'})
        if r.status_code == 200:
            d = r.json()
            return d.get('name') or d.get('username') or "Unknown"
    except: pass
    return "Unknown"

def validate_upload_id(base_url, upload_id, token):
    """Checks if Upload ID exists. Returns True/False."""
    url = base_url.rstrip('/') + f'/uploads/{upload_id}'
    headers = {'Authorization': f'Bearer {token}'}
    
    sys.stdout.write(f"🔍 Validating {upload_id}... ")
    sys.stdout.flush()
    
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            print("✔ Valid")
            return True
        elif r.status_code == 404:
            print("❌ Invalid Upload ID")
            return False
        else:
            print(f"⚠ Status {r.status_code} (Assuming valid)")
            return True
    except Exception as e:
        print(f"⚠ Check skipped: {e}")
        return True

def upload_zip_raw(base_url, upload_id, zip_path, token):
    zip_filename = os.path.basename(zip_path)
    folder_name = os.path.splitext(zip_filename)[0]

    url = (
        base_url.rstrip('/')
        + f'/uploads/{upload_id}/raw/{folder_name}'
        + '?overwrite_if_exists=true&auto_decompress=true'
    )

    print(f"⬆ Uploading {zip_filename} → folder '{folder_name}' ...")

    try:
        with open(zip_path, 'rb') as fh:
            headers = {
                'Authorization': f'Bearer {token}',
                "Content-Type": "application/octet-stream"
            }
            r = requests.put(url, headers=headers, data=fh, timeout=300)

        if r.status_code in (200, 201):
            # print("✔ Upload successful.")
            return folder_name
        else:
            print(f"❌ Upload failed: {r.status_code} - {r.text[:200]}")
            return None

    except Exception as e:
        print(f"❌ Network error: {e}")
        return None

def monitor_processing(base_url, upload_id, token, folder_name):
    """
    Monitors processing and filters output to only show files
    belonging to the current folder.
    """
    url = base_url.rstrip('/') + f'/uploads/{upload_id}'
    headers = {'Authorization': f'Bearer {token}'}
    print("\n⏳ Monitoring processing...")
    
    while True:
        try:
            time.sleep(2)
            r = requests.get(url, headers=headers)
            if r.status_code != 200: continue
            
            data = r.json().get('data', {})
            if data.get('process_running'):
                sys.stdout.write(f"\r   Status: {data.get('process_status')}   ")
                sys.stdout.flush()
                continue
            
            print(f"\n\n🏁 Finished: {data.get('process_status')}")
            
            ent_url = base_url.rstrip('/') + f'/uploads/{upload_id}/entries?page_size=100'
            r_ent = requests.get(ent_url, headers=headers)
            if r_ent.status_code == 200:
                entries = r_ent.json().get('data', [])
                
                # Filter for current folder
                current_entries = [
                    e for e in entries
                    if e.get('mainfile', '').startswith(f"{folder_name}/")
                ]
                
                if current_entries:
                    print("✔ Upload successful.")                
                
                else:
                    print("📊 No entries found for this upload yet ")
            break
        except KeyboardInterrupt:
            break
        except Exception:
            break

# -----------------------
# Main Loop
# -----------------------
def main():
    print("\n=== NOMAD UPLOADER ===\n")
    cfg = load_config()
    
    # 1. Config & User
    username = input("Enter NOMAD username: ").strip()
    if username not in cfg: cfg[username] = {}
    user_cfg = cfg[username]
    
    # Base URL Priority
    base_url = user_cfg.get('base_url')
    if base_url:
        print(f"Server: {base_url}")
        if not yn_prompt("Use this server?", "y"):
            base_url = input("Enter NOMAD Oasis URL: ").strip()
            user_cfg['base_url'] = base_url
            save_config(cfg)
    else:
        base_url = input("Enter NOMAD Oasis URL: ").strip()
        user_cfg['base_url'] = base_url
        save_config(cfg)
    
    token = authenticate(base_url, username)
    if not token: return
    full_name = get_user_name(base_url, token)
    print(f"Logged in as: {full_name}")
    
    # 2. Directories
    data_dir = user_cfg.get('directory_path', DEFAULT_DATA_DIR)
    print(f"\nData Directory: {data_dir}")
    if not yn_prompt("Use this directory?", "y"):
        data_dir = input("Enter new data directory: ").strip()
        user_cfg['directory_path'] = data_dir
        save_config(cfg)
    if not os.path.exists(data_dir): os.makedirs(data_dir)

    # --- Continuous Loop ---
    while True:
        print(f"\n--- New Measurement ---")
        
        sample_id = input("Enter Sample ID for this run: ").strip()
        if not sample_id:
            print("Sample ID required.")
            if not yn_prompt("Try again?", "y"): break
            continue

        ts = timestamp_now()
        safe_sid = sample_id.replace(" ", "_").replace("/", "-")
        folder_name = f"{ts}_{safe_sid}"
        local_folder = os.path.join(data_dir, folder_name)
        os.makedirs(local_folder, exist_ok=True)

        print(f"📁 Working Folder: {local_folder}")
        
        # Metafile copy
        meta_found = False
        for fn in os.listdir(data_dir):
            if "meta" in fn.lower() and fn.endswith(".txt"):
                src = os.path.join(data_dir, fn)
                dst = os.path.join(local_folder, f"{os.path.splitext(fn)[0]}_{ts}_{safe_sid}.txt")
                shutil.copy(src, dst)
                print(f"✔ Copied metafile: {os.path.basename(dst)}")
                meta_found = True
                break
        if not meta_found: print("⚠ No metafile found to copy.")

        print("\n👉 ACTION: Place your .xrdml (and .png, etc.) files into the folder above.")
        
        # Validation Loop: Ensure files exist before continuing
        xrdml_files = []
        while True:
            if not yn_prompt("Ready to process and upload?", "y"):
                if yn_prompt("Skip this measurement?", "y"):
                    xrdml_files = None
                    break
                continue

            # Check for files
            files = os.listdir(local_folder)
            xrdml_files = [f for f in files if f.lower().endswith(".xrdml") and "_processed" not in f]
            
            if xrdml_files:
                break
            else:
                print(f"\n⚠ WARNING: No .xrdml files found in: {local_folder}")
                print("   Please place the files in the folder and try again.")

        # If user chose to skip
        if xrdml_files is None:
            continue

        # Process files
        print(f"⚙ Processing {len(xrdml_files)} XRDML file(s)...")
        for f in xrdml_files:
            process_xrdml_binary(os.path.join(local_folder, f), full_name, sample_id, username)

        # NOW ask for Upload ID (after processing)
        upload_id = user_cfg.get('upload_id')
        
        while True:
            # Show saved ID if exists
            if upload_id:
                print(f"\nUpload ID: {upload_id}")
                if yn_prompt("Use this Upload ID?", "y"):
                    # Validate it
                    if validate_upload_id(base_url, upload_id, token):
                        break  # Valid, proceed to upload
                    else:
                        # Invalid, force new input
                        upload_id = None
                        continue
                else:
                    # User wants to enter new one
                    upload_id = None
                    continue
            
            # Manual input
            upload_id = input("\nEnter Upload ID: ").strip()
            if not upload_id:
                print("Upload ID required.")
                continue
            
            # Validate
            if validate_upload_id(base_url, upload_id, token):
                # Save valid ID
                user_cfg['upload_id'] = upload_id
                save_config(cfg)
                break
            else:
                # Invalid, loop back to input
                upload_id = None

        # Zip & Upload
        zip_path = create_flat_zip(local_folder, folder_name)
        if os.path.exists(zip_path):
            success = upload_zip_raw(base_url, upload_id, zip_path, token)
            if success:
                monitor_processing(base_url, upload_id, token, folder_name)
        
        if not yn_prompt("\nDo another measurement?", "n"):
            break

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nExiting.")
        sys.exit(0)