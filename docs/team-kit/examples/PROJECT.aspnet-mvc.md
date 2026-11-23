# Project: OrdersApp

> Example of a filled `docs/team/PROJECT.md` for an ASP.NET Core MVC application. Copy the layout, not the values.

## What this is
An internal web application where the sales team enters customer orders and the warehouse marks them shipped.

## Stack
- Language and version: C# 12 on .NET 8
- Framework: ASP.NET Core MVC (controllers and Razor views)
- Database: SQL Server through Entity Framework Core
- Front end: Razor views, Bootstrap 5, a little jQuery
- Other services: none

## Commands (run from the repository root)
| Task | Command |
|---|---|
| Install dependencies | `dotnet restore` |
| Build | `dotnet build --nologo` |
| Run locally | `dotnet run --project src/OrdersApp.Web` (listens on https://localhost:5001) |
| Test, everything | `dotnet test --nologo` |
| Test, one test or file | `dotnet test --nologo --filter "FullyQualifiedName~<TestName>"` |
| Lint | `dotnet format --verify-no-changes` |
| Format | `dotnet format` |
| Dependency audit | `dotnet list package --vulnerable --include-transitive` |

## Where things are
| What | Where |
|---|---|
| Application code | `src/OrdersApp.Web` (Controllers, Services, Data, Views) |
| Tests | `tests/OrdersApp.Tests`, one `<Class>Tests.cs` per class |
| Configuration | `src/OrdersApp.Web/appsettings.json`; example environment file: `.env.example` |
| Database migrations | `src/OrdersApp.Web/Migrations`; create one with `dotnet ef migrations add <Name> --project src/OrdersApp.Web` |
| Build and CI files | `.github/workflows/ci.yml` |
| Documentation | `README.md`, `docs/` |

## Conventions the code follows
- Naming: PascalCase for types and members, `_camelCase` for private fields, one type per file.
- Layers: Controllers call Services; Services call the `DbContext`. No database calls in controllers or views.
- Errors and logging: expected failures return a `Result`; unexpected ones are logged with `ILogger` and shown on the error page. Never log customer names or addresses.
- Tests: xUnit and FluentAssertions; the in-memory SQLite provider for data tests; fixtures in `tests/OrdersApp.Tests/Fixtures`.
- Commit messages: imperative, under 72 characters.

## Rules for this project
- Never touch: `Migrations/*.Designer.cs`, `wwwroot/lib`.
- Needs a person's sign-off, in addition to the team's list: any change to `Services/PricingService.cs`.
- Security notes: users sign in with the company's single sign-on; roles are `Sales` and `Warehouse`; customer addresses are personal data.

## Definition of done, in addition to the team's
- Every new action has an authorization test for a user in the wrong role.
- Every new view works with the keyboard alone.

## Team settings
- Review rounds per task: 2
- Test rounds per task: 3
- Base branch for diffs and release notes: main
- Gates that need a person: A (the PRD), B (sign-off items), C (a loop limit reached), D (an open critical or high security finding), E (before anything leaves the machine)
