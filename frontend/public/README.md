# Next.js static assets

Kept so the multi-stage Docker build's `COPY --from=build /app/public ./public`
resolves. Without this directory the frontend image fails to build with
"/app/public: not found", and CI never caught it because the workflow runs
`npm run build` (which does not need public/) and no `docker build`.
