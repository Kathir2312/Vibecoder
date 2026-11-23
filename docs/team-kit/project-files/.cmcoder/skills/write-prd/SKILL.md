---
name: write-prd
description: How to write a product requirements document that a developer can build and a tester can verify: problem, users, stories, testable acceptance criteria, out of scope, open questions. Use when turning a request into requirements.
---
# Writing a PRD

Use `template.md` in this skill. Write the file to `docs/team/prd/<slug>.md`.

## Steps
1. **Understand what exists.** Read the code and docs that touch the request. Note what the product already does, so you do not specify it again.
2. **State the problem** in two or three sentences: who has it, what hurts, why now. If you cannot, the request is not ready: say so in Open questions.
3. **Name the users** (roles, not people) and what each is trying to get done.
4. **Write user stories** as "As a <role> I want <action> so that <benefit>". One story, one action. Order them by value.
5. **Write acceptance criteria** for every story, numbered AC1, AC2, ... across the whole document.
   - Form: Given <state>, When <action>, Then <observable result>.
   - Use concrete values ("a 3-item order of 12.50 each shows a total of 37.50"), never "works correctly".
   - Include: the happy path, an invalid input, an empty state, a boundary (0, 1, max), and who may not do it.
   - Each must be checkable by a test without asking you.
6. **Non-functional needs** only when they matter here: speed (a number), security, accessibility, data retention, compatibility.
7. **Out of scope**: write down what someone might expect but that is not part of this work.
8. **Open questions**: split into *blocking* (the work cannot start) and *non-blocking* (say what you assumed, so it can be corrected).
9. **Success measure**: how will the team know it worked (a number or an observable change)?

## Quality check before you finish
- Could a developer who never spoke to you start tomorrow?
- Could a tester write a test for every AC from the text alone?
- Is anything in the acceptance criteria not in a story, or the reverse?
- Is the PRD under two pages? Cut what does not change a decision.
