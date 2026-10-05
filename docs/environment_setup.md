# Environment Setup & Recreation Guide for AutoKT

This document contains step-by-step instructions for recreating the development environment for both the **Backend** (FastAPI) and **Frontend** (Vite + React) from scratch.

---

## 1. Backend Environment Setup (Python / FastAPI)

### Requirements
- Python 3.11+
- pip

### Step-by-Step Instructions

1. **Navigate to the Backend Directory**:
   ```bash
   cd autokt/backend
   ```

2. **Create the Virtual Environment (`.venv`)**:
   ```bash
   python -m venv .venv
   ```

3. **Activate the Virtual Environment**:
   - **Windows (PowerShell)**:
     ```powershell
     .\.venv\Scripts\Activate.ps1
     ```
   - **Windows (CMD)**:
     ```cmd
     .\.venv\Scripts\activate.bat
     ```
   - **Linux / macOS**:
     ```bash
     source .venv/bin/activate
     ```

4. **Upgrade `pip` and Install Dependencies**:
   ```bash
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

5. **Verify the Installation (Pytest)**:
   ```bash
   pytest tests
   ```

6. **Run Backend API Server (Development)**:
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```
   - Swagger Documentation: `http://localhost:8000/docs`
   - Health Check Endpoint: `http://localhost:8000/health`

---

## 2. Frontend Environment Setup (Vite + React + TypeScript)

### Requirements
- Node.js (v18+)
- npm

### Step-by-Step Instructions

1. **Navigate to the Frontend Directory**:
   ```bash
   cd autokt/frontend
   ```

2. **Install Node Dependencies**:
   ```bash
   npm install
   ```

3. **Run Frontend Development Server**:
   ```bash
   npm run dev
   ```
   - Local App URL: `http://localhost:3000`

4. **Build for Production**:
   ```bash
   npm run build
   ```

5. **Preview Production Build**:
   ```bash
   npm run preview
   ```

---

## 3. Full-Stack Docker Containerized Environment

To run the entire AutoKT stack (Backend, Neo4j, ChromaDB) together:

```bash
cd autokt
docker-compose up --build
```

### Exposed Services:
- **Backend API**: `http://localhost:8000`
- **Health Check**: `http://localhost:8000/health`
- **Neo4j Browser**: `http://localhost:7474` (Bolt: `bolt://localhost:7687`)
- **ChromaDB Vector Service**: `http://localhost:8001`
