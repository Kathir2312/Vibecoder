---
name: design-ui
description: How to specify what users see and do: flow, screens, every state, exact text, validation, keyboard and screen-reader behaviour. Use for any change to a screen, a form, a message or a flow.
---
# Designing the user interface

Use `template.md` in this skill. Write the file to `docs/team/design/<slug>.md`.

## Steps
1. **Read the PRD** and list the stories that users see.
2. **Look at what exists.** Read the current screens, templates and styles. Note the components, class names, layout patterns and wording that the project already uses. Reuse them by name. A new pattern needs a reason.
3. **Draw the flow:** the steps a user takes, one line each, including where they start and where they end up, and what happens if they go back.
4. **Specify each screen:** its purpose, a simple layout (text or a table), the components on it (by their existing names), and what each control does.
5. **List every state** of each screen and component: empty, loading, error, success, disabled, long text, many items. Most defects live here.
6. **Write the exact text** for labels, buttons, headings, hints, empty states and error messages. Say what went wrong and what to do next. No "An error occurred".
7. **Validation:** for each field: required or not, format, limits, when it is checked (on typing, on leaving, on submit), and the message.
8. **Accessibility:** tab order, a visible focus, a label for every control, errors announced to screen readers, colour contrast at least AA (4.5:1 for text), nothing that only works with a mouse.
9. **Responsive:** what changes on a narrow screen.
10. **Tie it to the criteria:** for each screen, list the acceptance criteria it satisfies.

## Quality check
- Could a developer build every screen without asking what happens in an unusual state?
- Is every user-visible string written out?
- Does it look like the rest of the product?
