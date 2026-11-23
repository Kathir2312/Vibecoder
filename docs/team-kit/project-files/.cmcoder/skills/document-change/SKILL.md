---
name: document-change
description: How to update documentation after a change: which files to touch, style rules, how to verify every command and path, and the changelog entry. Use after a feature or a fix is built and tested.
---
# Documenting a change

## Steps
1. **Read what changed:** the PRD, the ADR and the code. Document what the code does now, not what the plan said.
2. **Find the places that must change.** Search the docs for the names of the changed things (commands, settings, endpoints, screens, functions). Check each of these:
   - the README: install, usage, configuration, examples that the change affects;
   - guides and reference pages that mention the changed behaviour;
   - the API reference or the OpenAPI file, if the project keeps one;
   - the example environment file and the configuration reference, for new settings;
   - the changelog (below);
   - the ADR: set its status to accepted when the change is built;
   - the PRD: set its status to done.
3. **Write for the reader** who has to use the feature tomorrow: what it is for, how to use it, one real example, and what can go wrong with the message they will see.
4. **Verify everything you write.** Each command, path, option, endpoint and setting name must exist in the code or the config. Run the commands that are safe to run. Copy output; do not invent it.
5. **Remove what is now false.** Old instructions left next to new ones are worse than none.
6. **Changelog:** add one entry under "Unreleased" in the project's format (Keep a Changelog if there is none): a short line for users, in the category Added, Changed, Fixed, Removed or Security. Mention breaking changes first.

## Style
- Short sentences, active voice, present tense. One idea per paragraph.
- Examples before explanations. Real values, not `foo`.
- Headings that say what the reader can do ("Export orders"), not what the document is.
- No marketing words. No "simply" or "just".

## Quality check
- Is every command, path and setting name real?
- Would someone new succeed using only this page?
- Is anything in the docs now false?
