# Matcha Distributed Agent

This repository contains the provider-side client for the **Matcha Kolektif** network. The agent enables local hardware (CPU/GPU) to register as a compute node, broadcast telemetry, and execute containerized research tasks via Docker.

---

## Prerequisites

* **Python 3.10+**
* **Docker Desktop:** Must be running to execute research tasks.
* **NVIDIA Drivers:** Required for GPU-accelerated tasks (RTX series recommended).
* **Git:** To clone the repository.

---

## Installation

### 1. Clone the repository
```bash
git clone https://github.com/ruasnv/matcha-agent.git
cd matcha-agent
```


### 2. Set up a Virtual Environment
```Bash
python -m venv venv
# Linux/macOS:
source venv/bin/activate  
# Windows:
venv\Scripts\activate
```

### 3. Install Dependencies
```Bash
pip install -r requirements.txt
```


## Configuration
The agent requires an .env file in the root directory. This file is automatically updated during the enrollment process, but initial server communication requires the following variables:

```Bash
ORCHESTRATOR_URL= https://matcha-orchestrator.onrender.com
ORCHESTRATOR_API_KEY_PROVIDERS=your_provided_api_key
```

## Usage

### 1. Enrollment
Link your local hardware to your Matcha account using the unique token generated in the web dashboard:

```Bash
python agent.py --enroll <TOKEN>
```

### 2. Execution
Once enrolled, start the agent to begin broadcasting telemetry and listening for compute tasks:
```Bash
python agent.py
```

## Technical Architecture
The agent performs three primary functions:

- Telemetry: Polls psutil and pynvml every 5 seconds to report system load to the orchestrator.

- Task Management: Long-polls the orchestrator for queued jobs matching the node's hardware profile.

- Containerization: Pulls required Docker images and executes research scripts in isolated environments, mounting /outputs for artifact collection.
