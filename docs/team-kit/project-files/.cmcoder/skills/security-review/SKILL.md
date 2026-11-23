---
name: security-review
description: How to review a whole change for security: injection, authentication and authorization, secrets, data exposure, dependencies, configuration. Severity scale and report format. Use before a release or for any change that handles input, data or permissions.
---
# Security review

Write the report with `report-template.md` to `docs/team/reviews/<slug>-security.md`.

## Steps
1. **Learn what is new:** from the PRD and the ADR, list the new inputs, stored data, permissions, endpoints, files, external calls and dependencies. Review those first.
2. **Read the whole change** (all tasks together), not only the last diff.
3. **Go through the checklist** below. For each item write what you checked, even when you found nothing.
4. **Dependencies:** run the audit command from `docs/team/PROJECT.md` if there is one, and list new or upgraded packages with a reason to trust them.
5. **Secrets:** search the change (and the files it adds) for keys, tokens, passwords, connection strings and private keys. Never copy one into the report: give the file and line.
6. **Report** each finding: severity, `path:line`, how it could be abused (one or two sentences), the fix.

## Checklist
- **Injection:** SQL built from strings; commands built from input; HTML or JavaScript built from input (cross-site scripting); file paths built from input (path traversal); XML or deserialization of untrusted data; template injection.
- **Authentication:** how users prove who they are; password storage; session or token lifetime; logout; brute-force limits.
- **Authorization:** is every action checked on the server, for this user and this object? Can a user change an id and reach someone else's data? Can a normal user call an admin function directly?
- **Input handling:** validated at the boundary for type, length and range; file uploads limited in type and size and stored outside the web root; redirects only to allowed places.
- **Data exposure:** responses that return more fields than needed; secrets or personal data in logs, errors or URLs; verbose error messages that reveal internals.
- **Web:** cross-site request forgery protection on state-changing requests; cookies marked secure, http-only and same-site; cross-origin settings no wider than needed; security headers.
- **Cryptography:** no home-made crypto; no weak or obsolete algorithms; no fixed keys or IVs; random values from a secure source.
- **Configuration:** no debug mode, default credentials or open ports in the new settings; least privilege for service accounts and database users.
- **Dependencies:** known vulnerable versions; packages from unexpected sources; install scripts.
- **Denial of service:** unbounded loops, results, uploads or requests; expensive work reachable without login.

## Severity
- **critical:** remote takeover, authentication bypass, all data exposed. Block the release.
- **high:** one user can reach another's data or do an admin action; injection that works. Block the release.
- **medium:** needs unusual conditions or limited impact. Fix soon.
- **low:** hardening, defence in depth.

## Rules
- Show what you can show: a line of code and a scenario. If you cannot explain how it is abused, it is not a finding.
- Do not exploit anything, scan hosts, or call external services other than the audit command.
- Say what you did not check.
