// The agent's todo list as the panel shows it: counts, the task in progress, numbered rows.
// Plain code shared by the chat panel's compact bar and the Tasks tab.

export type TodoStatus = "pending" | "in_progress" | "completed";

export interface TodoRow {
  no: number; // 1-based, in the order the agent wrote them
  status: TodoStatus;
  text: string;
}

export interface TodoSummary {
  rows: TodoRow[];
  total: number;
  done: number;
  active: number;
  pending: number;
  /** What it is working on: the task in progress, else the next one, else null (all done). */
  current: TodoRow | null;
  percent: number; // done / total, 0-100
}

function statusOf(value: unknown): TodoStatus {
  return value === "completed" || value === "in_progress" ? value : "pending";
}

export function summarizeTodos(todos: readonly Record<string, unknown>[]): TodoSummary {
  const rows: TodoRow[] = todos.map((t, i) => ({
    no: i + 1,
    status: statusOf(t.status),
    text: String(t.content ?? t.activeForm ?? "").trim() || "(untitled task)",
  }));
  const count = (s: TodoStatus) => rows.filter((r) => r.status === s).length;
  const done = count("completed");
  return {
    rows,
    total: rows.length,
    done,
    active: count("in_progress"),
    pending: count("pending"),
    current: rows.find((r) => r.status === "in_progress") ?? rows.find((r) => r.status === "pending") ?? null,
    percent: rows.length ? Math.round((done / rows.length) * 100) : 0,
  };
}

export const TODO_MARKS: Record<TodoStatus, string> = { pending: "☐", in_progress: "►", completed: "☑" };
