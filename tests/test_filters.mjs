import { test } from "node:test";
import assert from "node:assert/strict";
import { state, matchesOrganization } from "../app/static/core.js";

const resources = [
  { id: "incus:app", project_id: "app", tag_ids: ["live", "web"] },
  { id: "libvirt:app", project_id: "app", tag_ids: ["dev"] },
  { id: "incus:database", project_id: "other", tag_ids: ["live"] },
  { id: "incus:network", project_id: "app", tag_ids: [] },
  { id: "libvirt:network", project_id: null, tag_ids: ["live"] },
  { id: "incus:unassigned", project_id: null, tag_ids: [] },
];
const filter = (project, tag) => {
  state.project = project;
  state.tag = tag;
  return resources.filter(matchesOrganization).map((r) => r.id);
};
test("project view includes only assigned VMs and networks across providers", () => {
  assert.deepEqual(filter("app", "all"), [
    "incus:app",
    "libvirt:app",
    "incus:network",
  ]);
});
test("tag and project filters intersect and tags may cross projects", () => {
  assert.deepEqual(filter("all", "live"), [
    "incus:app",
    "incus:database",
    "libvirt:network",
  ]);
  assert.deepEqual(filter("app", "live"), ["incus:app"]);
  assert.deepEqual(filter("other", "web"), []);
});
test("unassigned and untagged views work together and individually", () => {
  assert.deepEqual(filter("unassigned", "all"), [
    "libvirt:network",
    "incus:unassigned",
  ]);
  assert.deepEqual(filter("app", "untagged"), ["incus:network"]);
  assert.deepEqual(filter("unassigned", "untagged"), ["incus:unassigned"]);
});
test("clearing filters restores the entire inventory", () => {
  assert.equal(filter("all", "all").length, resources.length);
});
