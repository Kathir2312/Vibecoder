# Project: <<name>>

> Every teammate reads this file before every task. Fill the lines that contain `<<…>>` once, keep the file short and true, and fix it when the project changes. A line that still contains `<<` counts as missing: the tech lead will ask you for it.

## What this is
<<one or two sentences: what the product does and for whom>>

## Stack
- Language and version: <<e.g. C# 12 on .NET 8>>
- Framework: <<e.g. ASP.NET Core MVC>>
- Database: <<e.g. SQL Server through Entity Framework Core, or "none">>
- Front end: <<e.g. Razor views and jQuery, or React 18 with TypeScript>>
- Other services: <<queues, caches, external APIs, or "none">>

## Commands (run from the repository root)
| Task | Command |
|---|---|
| Install dependencies | <<command>> |
| Build | <<command>> |
| Run locally | <<command, and the address it listens on>> |
| Test, everything | <<command>> |
| Test, one test or file | <<command with a placeholder for the name>> |
| Lint | <<command, or "none">> |
| Format | <<command, or "none">> |
| Dependency audit | <<command, or "none">> |

## Where things are
| What | Where |
|---|---|
| Application code | <<path>> |
| Tests | <<path, and how test files are named>> |
| Configuration | <<path>>; example environment file: <<path, or "none">> |
| Database migrations | <<path, and the command that creates one, or "none">> |
| Build and CI files | <<path>> |
| Documentation | <<path>> |

## Conventions the code follows
- Naming: <<e.g. PascalCase types, camelCase locals, one class per file>>
- Layers: <<e.g. Controllers → Services → Repositories; no database calls in controllers>>
- Errors and logging: <<how errors are raised and handled, which logger, what is never logged>>
- Tests: <<framework, style, where fixtures live>>
- Commit messages: <<style>>

## Rules for this project
- Never touch: <<generated files, vendored code, legacy modules, or "nothing">>
- Needs a person's sign-off, in addition to the team's list: <<e.g. any change to the payments module, or "nothing extra">>
- Security notes: <<how users log in, the roles, where sensitive data is, or "none">>

## Definition of done, in addition to the team's
- <<e.g. every new screen can be used with the keyboard; every new endpoint has an authorization test; or "nothing extra">>

## Team settings
- Review rounds per task: 2
- Test rounds per task: 3
- Base branch for diffs and release notes: <<main>>
- Gates that need a person: A (the PRD), B (sign-off items), C (a loop limit reached), D (an open critical or high security finding), E (before anything leaves the machine)
