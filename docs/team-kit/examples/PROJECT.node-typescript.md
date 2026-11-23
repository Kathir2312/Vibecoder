# Project: Booking API

> Example of a filled `docs/team/PROJECT.md` for a Node.js and TypeScript service. Copy the layout, not the values.

## What this is
A REST API that lets a mobile app book and cancel appointments at clinics.

## Stack
- Language and version: TypeScript 5 on Node.js 22
- Framework: Express 4
- Database: PostgreSQL through Prisma
- Front end: none (a separate mobile app calls this API)
- Other services: Redis for rate limiting, an email provider's HTTP API

## Commands (run from the repository root)
| Task | Command |
|---|---|
| Install dependencies | `npm ci` |
| Build | `npm run build` |
| Run locally | `npm run dev` (listens on http://localhost:3000) |
| Test, everything | `npm test` |
| Test, one test or file | `npx vitest run <path or name>` |
| Lint | `npm run lint` |
| Format | `npm run format` |
| Dependency audit | `npm audit --omit=dev` |

## Where things are
| What | Where |
|---|---|
| Application code | `src/` (`routes`, `services`, `repositories`, `middleware`) |
| Tests | next to the code, as `<name>.test.ts`; end-to-end tests in `test/e2e` |
| Configuration | `src/config.ts` reads the environment; example environment file: `.env.example` |
| Database migrations | `prisma/migrations`; create one with `npx prisma migrate dev --name <name>` |
| Build and CI files | `.github/workflows/ci.yml`, `Dockerfile` |
| Documentation | `README.md`, `docs/`, `openapi.yaml` |

## Conventions the code follows
- Naming: camelCase for values and functions, PascalCase for types, kebab-case file names.
- Layers: routes validate input (zod) and call services; services hold the rules; repositories are the only code that uses Prisma.
- Errors and logging: throw `AppError` with a status and a code; one error middleware turns it into JSON; log with `pino`, never log tokens or email addresses.
- Tests: vitest; every route has a test for success, bad input and a missing token.
- Commit messages: Conventional Commits (`feat:`, `fix:`, `docs:`).

## Rules for this project
- Never touch: `prisma/migrations` files that are already merged, `src/generated`.
- Needs a person's sign-off, in addition to the team's list: any change to `openapi.yaml` that removes or renames a field.
- Security notes: callers send a JWT; a user may only read and cancel their own bookings; clinic staff have the `staff` role.

## Definition of done, in addition to the team's
- `openapi.yaml` matches the routes.
- A new route is covered by an authorization test (wrong user, missing token).

## Team settings
- Review rounds per task: 2
- Test rounds per task: 3
- Base branch for diffs and release notes: main
- Gates that need a person: A (the PRD), B (sign-off items), C (a loop limit reached), D (an open critical or high security finding), E (before anything leaves the machine)
