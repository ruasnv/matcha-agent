import os
import shutil
import tempfile
import time
import requests
import wsgiref.headers
import docker
import json
import uuid
import psutil
import argparse
import signal
import sys
from dotenv import load_dotenv
from docker.types import DeviceRequest

# --- 1. GLOBAL INITIALIZATION ---
load_dotenv()

try:
    import pynvml 
    pynvml.nvmlInit()
    HAS_GPU = True
except:
    HAS_GPU = False

# Fix for certain environments where wsgiref might be finicky
if not hasattr(wsgiref.headers.Headers, 'items'):
    wsgiref.headers.Headers.items = lambda self: self._headers

def get_unique_device_id():
    node_id = hex(uuid.getnode()) 
    return f"matcha-{node_id}"

PROVIDER_ID = os.getenv("PROVIDER_ID") or get_unique_device_id()
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "https://matcha-orchestrator.onrender.com")

def get_auth_headers():
    return {
        "X-API-Key": os.getenv("ORCHESTRATOR_API_KEY_PROVIDERS", "debug-provider-key"),
        "Content-Type": "application/json"
    }

# --- Docker Initialization with User-Friendly Error ---
try:
    client = docker.from_env()
    # This line triggers a connection test immediately
    client.ping() 
except Exception as e:
    print("\n" + "!"*60)
    print("---DOCKER NOT DETECTED---")
    print("Matcha Agent requires Docker Desktop to run research tasks.")
    print("\nPLEASE:")
    print("1. Open Docker Desktop.")
    print("2. Wait for the whale icon to turn solid green.")
    print("3. Restart this agent.")
    print("!"*60 + "\n")
    sys.exit(1)

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

 # --- 3. SIGNAL HANDLING ---
def signal_handler(sig, frame):
    """Gracefully shuts down the agent and notifies the server."""
    print("\n🛑 Disconnecting from Matcha Kolektif...")
    try:
        # We notify the server that this provider is going offline
        requests.post(
            f"{ORCHESTRATOR_URL}/provider/heartbeat", 
            json={"provider_id": PROVIDER_ID, "telemetry": {"status": "offline"}},
            headers=get_auth_headers(),
            timeout=2
        )
    except:
        pass
    print("👋 Goodbye!")
    sys.exit(0)

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
        if response.status_code != 200:
            return False
            
        data = response.json()
        task = data.get("task")
        if not task: return False

        task_id = task['task_id']
        upload_url = task.get('upload_url') 
        print(f"📦 Assigned Task: {task_id}")
        
        result_dir = tempfile.mkdtemp()
        
        try:
            print(f"DEBUG: Launching runner for {task_id}...")
            container = client.containers.run(
            "ruasnv/matcha-runner:latest", 
            detach=True,
            environment={
                "PROJECT_URL": task.get('input_path'),
                "SCRIPT_PATH": task.get('script_path', 'main.py')
            },
            volumes={result_dir: {'bind': '/outputs', 'mode': 'rw'}},
            network_mode="host",
            # 🚀 THE CRITICAL ADDITION: Request all GPUs
            device_requests=[
                DeviceRequest(count=-1, capabilities=[['gpu']])
            ]
        )
            
            update_task_status(task_id, "RUNNING")
            
            # 🚀 REAL-TIME LOG STREAMING
            full_logs = ""
            print("--- DOCKER START ---")
            for line in container.logs(stream=True, follow=True):
                chunk = line.decode('utf-8')
                print(f"🐳 {chunk.strip()}")
                full_logs += chunk
            print("--- DOCKER END ---")

            result = container.wait(timeout=300) 
            
            if result['StatusCode'] == 0:
                print(f"✅ Execution finished. Preparing upload...")
                
                # Zip the result directory if it has files
                if os.listdir(result_dir) and upload_url:
                    zip_path = shutil.make_archive(f"results_{task_id}", 'zip', result_dir)
                    
                    with open(zip_path, 'rb') as f:
                        upload_res = requests.put(
                            upload_url, 
                            data=f,
                            headers={'Content-Type': 'application/zip'}
                        )
                    
                    if upload_res.status_code == 200:
                        print("📤 Results uploaded successfully via secure tunnel.")
                    os.remove(zip_path)

                update_task_status(task_id, "COMPLETED", full_logs)
                print(f"✨ Task {task_id} fully completed.")
            else:
                print(f"❌ Container exited with code {result['StatusCode']}")
                update_task_status(task_id, "FAILED", full_logs)
            
            container.remove()

        except Exception as e:
            print(f"❌ Execution Error: {e}")
            update_task_status(task_id, "FAILED", str(e))
            try:
                # Attempt to kill the container if it's still hanging
                c = client.containers.get(container.id)
                c.stop()
                c.remove()
            except:
                pass
        finally:
            if os.path.exists(result_dir): 
                shutil.rmtree(result_dir)
        
        return True

    except Exception as e:
        print(f"❌ Polling Error: {e}")
        return False


# --- 5. EXECUTION ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--enroll", help="The token from your Matcha Dashboard")
    args = parser.parse_args()

    # 1. Set the signal trap immediately
    signal.signal(signal.SIGINT, signal_handler)
    
    if args.enroll:
        enroll_device(args.enroll)
    
    if not os.getenv("USER_ID") and not os.path.exists(".env"):
        print("🛑 ERROR: USER_ID not found. Run: python agent.py --enroll <token>")
        sys.exit(1)

    register_provider()
    
    last_heartbeat = 0
    while True:
        # Heartbeat every 10 seconds to keep the dashboard status 'Active'
        if time.time() - last_heartbeat >= 10:
            send_heartbeat()
            last_heartbeat = time.time()
            
        poll_for_task()
        time.sleep(2) # Poll every 2 seconds for low latency