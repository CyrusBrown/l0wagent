// A minimal in-memory D1 for tests, over node:sqlite, with this hub's migrations applied.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

const MIGRATIONS = join(import.meta.dirname, "..", "migrations");

class Stmt {
  private args: unknown[] = [];
  constructor(
    private db: DatabaseSync,
    private sql: string,
  ) {}
  bind(...a: unknown[]): this {
    this.args = a;
    return this;
  }
  // D1 numbers its parameters (?1, ?2); node:sqlite takes them positionally in the same way
  private st() {
    return this.db.prepare(this.sql);
  }
  async first<T>(): Promise<T | null> {
    return (this.st().get(...(this.args as never[])) as T | undefined) ?? null;
  }
  async all<T>(): Promise<{ results: T[] }> {
    return { results: this.st().all(...(this.args as never[])) as T[] };
  }
  async run(): Promise<{ meta: { changes: number } }> {
    const r = this.st().run(...(this.args as never[]));
    return { meta: { changes: Number(r.changes) } };
  }
}

export function fakeD1(): D1Database {
  const db = new DatabaseSync(":memory:");
  for (const f of readdirSync(MIGRATIONS).filter((x) => x.endsWith(".sql")).sort()) db.exec(readFileSync(join(MIGRATIONS, f), "utf8"));
  return {
    prepare: (sql: string) => new Stmt(db, sql),
    batch: async (stmts: Stmt[]) => Promise.all(stmts.map((s) => s.run())),
  } as unknown as D1Database;
}
