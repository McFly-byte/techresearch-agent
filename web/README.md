# web/ (TechResearch Agent frontend)

Phase 0: minimal Vite + React 18 + TypeScript shell.

```powershell
npm install
npm run dev        # http://localhost:5173
npm run typecheck
npm run build
```

The dev server proxies `/api/*` to `http://localhost:8000` (FastAPI).
