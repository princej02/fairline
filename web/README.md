# Fairline web

Next.js (App Router) + TypeScript + Tailwind + TanStack Query + Lightweight Charts.

```bash
npm install
npm run dev        # http://localhost:3100, proxies /api/* to the API
```

Start the API first (`make api` from the repo root). Set `FAIRLINE_API_URL` if it isn't on
`http://127.0.0.1:8000`. Checks: `npm run typecheck`, `npm run lint`, `npm run build`.
