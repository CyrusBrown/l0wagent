import { describe, expect, it } from "vitest";
import { checkName, isSpecReserved, lookalikeKey } from "../src/proto/names";
import * as P from "../src/proto/payloads";

const taken = new Set(["mara", "opsname"]);
const checks = (bound: boolean) => ({
  bound,
  reservedExtra: ["opsname", "shop"],
  isTaken: async (n: string) => taken.has(n) && n !== "opsname",
  lookalikeTaken: async (k: string) => k === lookalikeKey("mara"),
});

describe("bound invites (names)", () => {
  it("a reserved extra name (and its lookalikes) is refused unbound and accepted bound", async () => {
    await expect(checkName("opsname", checks(false))).rejects.toMatchObject({ code: "name_reserved" });
    await expect(checkName("ops-name", checks(false))).rejects.toMatchObject({ code: "name_reserved" });
    expect(await checkName("opsname", checks(true))).toBe("opsname");
    expect(await checkName("ops-name", checks(true))).toBe("ops-name");
  });

  it("the spec's fixed list, taken names and their lookalikes are refused even when bound", async () => {
    await expect(checkName("admin", checks(true))).rejects.toMatchObject({ code: "name_reserved" });
    await expect(checkName("adm1n", checks(true))).rejects.toMatchObject({ code: "name_reserved" });
    await expect(checkName("mara", checks(true))).rejects.toMatchObject({ code: "name_taken" });
    await expect(checkName("ma-ra", checks(true))).rejects.toMatchObject({ code: "name_taken" });
  });

  it("isSpecReserved", () => {
    expect(isSpecReserved("admin")).toBe(true);
    expect(isSpecReserved("adm1n")).toBe(true);
    expect(isSpecReserved("opsname")).toBe(false);
  });

  it("the invite lookup answer carries name_bound", () => {
    expect(P.buildInviteResponse("2026-11-24T21:30:00Z", "Hub")).toMatchObject({ name_hint: null, name_bound: false });
    expect(P.buildInviteResponse("2026-11-24T21:30:00Z", "Hub", "opsname", true)).toMatchObject({ name_hint: "opsname", name_bound: true });
  });
});
