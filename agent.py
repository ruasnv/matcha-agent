import os
import shutil
import tempfile
import time
import requests
import wsgiref.headers
import docker
import json
import boto3
import uuid
import psutil
import argparse
from botocore.config import Config
from dotenv import load_dotenv

# --- 1. GLOBAL INITIALIZATION ---
load_dotenv()

try:
    import pynvml 
    pynvml.nvmlInit()
    HAS_GPU = True
except:
    HAS_GPU = False

if not hasattr(wsgiref.headers.Headers, 'items'):
    wsgiref.headers.Headers.items = lambda self: self._headers

def get_unique_device_id():
    """Generates a persistent hardware fingerprint based on the MAC address."""
    node_id = hex(uuid.getnode()) 
    return f"matcha-{node_id}"

# Identity and Config
PROVIDER_ID = os.getenv("PROVIDER_ID") or get_unique_device_id()
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "https://matcha-orchestrator.onrender.com")

def get_auth_headers():
    """Refreshes headers to include the latest API keys from .env"""
    return {
        "X-API-Key": os.getenv("ORCHESTRATOR_API_KEY_PROVIDERS", "debug-provider-key"),
        "Content-Type": "application/json"
    }

client = docker.from_env()

# S3/R2 Setup
s3_client = boto3.client(
    's3',
    endpoint_url=os.getenv('R2_ENDPOINT_URL'),
    aws_access_key_id=os.getenv('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('R2_SECRET_ACCESS_KEY'),
    config=Config(signature_version='s3v4', region_name='auto')
)

# --- 2. HARDWARE DETECTION ---
GPU_HANDLE = None
GPU_NAME = "Unknown GPU"

if HAS_GPU:
    try:
        pynvml.nvmlInit()
        GPU_HANDLE = pynvml.nvmlDeviceGetHandleByIndex(0)
        name_raw = pynvml.nvmlDeviceGetName(GPU_HANDLE)
        GPU_NAME = name_raw.decode('utf-8') if isinstance(name_raw, bytes) else str(name_raw)
        print(f"✅ Dynamic Hardware Detection: Found {GPU_NAME}")
    except Exception as e:
        print(f"⚠️ GPU Initialization failed: {e}")
        HAS_GPU = False

def get_gpu_specs():
    gpus = []
    if not HAS_GPU: return gpus
    try:
        device_count = pynvml.nvmlDeviceGetCount()
        for i in range(device_count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name_raw = pynvml.nvmlDeviceGetName(handle)
            name = name_raw.decode('utf-8') if isinstance(name_raw, bytes) else str(name_raw)
            gpus.append({"id": f"gpu_{i}", "name": name, "status": "idle"})
    except:
        pass
    return gpus

def get_telemetry():
    cpu_usage = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    telemetry = {
        "cpu_load": cpu_usage,
        "ram_used_gb": round(ram.used / (1024**3), 2),
        "ram_total_gb": round(ram.total / (1024**3), 2),
        "status": "idle",
        "gpu": None
    }
    if GPU_HANDLE:
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(GPU_HANDLE)
            mem = pynvml.nvmlDeviceGetMemoryInfo(GPU_HANDLE)
            telemetry["gpu"] = {
                "name": GPU_NAME,
                "load": int(util.gpu),
                "vram_used": round(mem.used / (1024**3), 2),
                "vram_total": round(mem.total / (1024**3), 2)
            }
        except:
            telemetry["gpu"] = {"name": GPU_NAME, "load": 0, "status": "offline"}
    return telemetry

# --- 3. ENROLLMENT ---
def save_credentials(user_id):
    """Automatically persists the user_id to the local environment."""
    with open(".env", "a") as f:
        f.write(f"\nUSER_ID={user_id}")
        f.write(f"\nPROVIDER_ID={PROVIDER_ID}")
    # Force reload the environment variables for the current process
    os.environ["USER_ID"] = user_id
    os.environ["PROVIDER_ID"] = PROVIDER_ID
    print(f"📝 Credentials saved. Identity: {PROVIDER_ID}")

def enroll_device(token):
    print(f"🔑 Linking device {PROVIDER_ID} to Matcha Kolektif...")
    try:
        res = requests.post(
            f"{ORCHESTRATOR_URL}/provider/enroll", 
            json={"token": token, "provider_id": PROVIDER_ID},
            headers=get_auth_headers()
        )
        if res.status_code == 200:
            uid = res.json().get('user_id')
            save_credentials(uid)
            print(f"✅ Enrollment complete! Node linked to account.")
            exit(0)
        else:
            print(f"❌ Error: {res.json().get('error')}")
            exit(1)
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        exit(1)

# --- 4. NETWORKING & TASKS ---
def register_provider():
    url = f"{ORCHESTRATOR_URL}/provider/register"
    payload = {
        "provider_id": PROVIDER_ID,
        "user_id": os.getenv("USER_ID"),
        "hardware_specs": get_telemetry(),
        "gpus": get_gpu_specs() 
    }
    try:
        res = requests.post(url, json=payload, headers=get_auth_headers())
        res.raise_for_status()
        print(f"🚀 Online as {PROVIDER_ID} ({GPU_NAME})")
    except Exception as e:
        print(f"❌ Registration failed: {e}")
        exit(1)

def send_heartbeat():
    url = f"{ORCHESTRATOR_URL}/provider/heartbeat"
    payload = {"provider_id": PROVIDER_ID, "telemetry": get_telemetry()}
    try:
        requests.post(url, json=payload, headers=get_auth_headers())
    except:
        pass

def update_task_status(task_id, status, logs=None, result_url=None):
    url = f"{ORCHESTRATOR_URL}/provider/task_update"
    payload = {
        "task_id": task_id,
        "status": status,
        "result_url": result_url,
        "details": {"stdout": logs}
    }
    try:
        requests.post(url, json=payload, headers=get_auth_headers())
    except Exception as e:
        print(f"Failed to update task: {e}")

def poll_for_task():
    url = f"{ORCHESTRATOR_URL}/provider/get_task"
    try:
        response = requests.post(url, json={"provider_id": PROVIDER_ID}, headers=get_auth_headers())
        data = response.json()
        task = data.get("task")
        if not task: return False

        task_id = task['task_id']
        print(f"📦 Assigned Task: {task_id}")
        result_dir = tempfile.mkdtemp()
        
        try:
            print("🏗️ Booting Docker Runner...")
            container = client.containers.run(
                "runner:latest", 
                detach=True,
                environment={
                    "PROJECT_URL": task.get('input_path'),
                    "SCRIPT_PATH": task.get('script_path', 'main.py')
                },
                volumes={result_dir: {'bind': '/outputs', 'mode': 'rw'}}
            )
            update_task_status(task_id, "RUNNING")
            
            result = container.wait()
            logs = container.logs().decode('utf-8')
            
            if result['StatusCode'] == 0:
                result_url = None
                if os.listdir(result_dir):
                    zip_name = f"results_{task_id}"
                    shutil.make_archive(zip_name, 'zip', result_dir)
                    artifact_key = f"artifacts/{task_id}.zip"
                    with open(f"{zip_name}.zip", 'rb') as f:
                        s3_client.upload_fileobj(f, os.getenv('R2_BUCKET_NAME'), artifact_key)
                    
                    result_url = s3_client.generate_presigned_url(
                        'get_object', 
                        Params={'Bucket': os.getenv('R2_BUCKET_NAME'), 'Key': artifact_key}, 
                        ExpiresIn=604800
                    )
                    os.remove(f"{zip_name}.zip")
                update_task_status(task_id, "COMPLETED", logs, result_url=result_url)
                print(f"✅ Task {task_id} Success.")
            else:
                update_task_status(task_id, "FAILED", logs)
            container.remove()
        except Exception as e:
            update_task_status(task_id, "FAILED", str(e))
        finally:
            if os.path.exists(result_dir): shutil.rmtree(result_dir)
        return True
    except:
        return False

# --- 5. EXECUTION ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--enroll", help="The 6-digit token from your Matcha Dashboard")
    args = parser.parse_args()

    if args.enroll:
        enroll_device(args.enroll)
    
    if not os.getenv("USER_ID"):
        print("🛑 ERROR: USER_ID not found. Run: python agent.py --enroll <token>")
        exit(1)

    register_provider()
    last_heartbeat = 0
    while True:
        if time.time() - last_heartbeat >= 5:
            send_heartbeat()
            last_heartbeat = time.time()
        poll_for_task()
        time.sleep(1)