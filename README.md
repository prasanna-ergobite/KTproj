# AutoKT - AI-Powered Knowledge-Transfer Platform

AutoKT is an AI-powered platform for automated knowledge transfer, code analysis, document ingestion, and developer onboarding pack generation using graph databases (Neo4j) and vector databases (ChromaDB).

## Repository Structure

```text
autokt/
├── docker-compose.yml           # FastAPI + Neo4j + Chroma services
├── .env.example                 # Environment variable template
├── .gitignore
├── README.md                    # Project overview & usage instructions
├── backend/                     # FastAPI application backend
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py              # FastAPI entry point & route registration
│   │   ├── api/                 # API endpoint routers
│   │   ├── graphs/              # LangGraph workflows
│   │   ├── db/                  # Neo4j and Chroma database clients
│   │   ├── models/              # Pydantic data schemas
│   │   └── core/                # Configuration and Auth utilities
│   └── tests/                   # Pytest test suite
└── frontend/                    # Vite + React frontend application
    ├── package.json
    ├── vite.config.ts
    ├── index.html
    ├── tsconfig.json
    ├── tailwind.config.js
    ├── postcss.config.js
    └── src/
        ├── main.tsx
        ├── App.tsx
        ├── index.css
        ├── components/
        └── pages/
```

## How to Run

### 1. Docker Compose (Full Stack Environment)

Start all services (Backend, Neo4j, ChromaDB):

```bash
cd autokt
docker-compose up --build
```

Services exposed:
- **Backend API**: http://localhost:8000
- **Health Check**: http://localhost:8000/health
- **Neo4j Browser**: http://localhost:7474 (Bolt: `bolt://localhost:7687`)
- **ChromaDB API**: http://localhost:8001

### 2. Backend Local Development

```bash
cd autokt/backend
python -m venv venv
# On Windows:
.\venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Run tests:
```bash
pytest
```

### 3. Frontend Local Development

```bash
cd autokt/frontend
npm install
npm run dev
```

Frontend will be available at http://localhost:3000.
