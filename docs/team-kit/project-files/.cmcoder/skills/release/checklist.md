# Release checklist: <version>

Mark each line ✓ (with the result), ✗ (with the reason) or n/a.

## Code
- [ ] Build passes: `<command>` → <result>
- [ ] Whole test suite passes: `<command>` → <n passed, n skipped>
- [ ] Linter and formatter pass: `<command>` → <result>
- [ ] Working tree is clean (`git status`), and the branch is the one meant for release
- [ ] Version number updated where the project keeps it: <files>

## Review
- [ ] Every task of this release was approved by the reviewer and passed by the tester
- [ ] Security report exists for these changes: `docs/team/reviews/<slug>-security.md`
- [ ] No open critical or high finding

## Compatibility
- [ ] Breaking changes listed in the notes, with the steps to adapt
- [ ] Database migrations run on a copy of production-like data, forwards and backwards
- [ ] New settings and environment variables listed (names only) and present in the example environment file

## Documentation
- [ ] Changelog entry written
- [ ] README and docs match the code
- [ ] Release notes written for users

## Ready to run (by a person)
- [ ] Commands listed in order, each with its undo command
- [ ] Backup step listed where data changes
- [ ] Smoke test written, with the rollback condition
