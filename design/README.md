# Frontend design provenance

[Figma: CarrTech VM Manager](https://www.figma.com/design/lLfm0bybtcMJEy5yBfd9nb?node-id=2-139)

Simon explicitly requested Claude Code for frontend design. Claude Code authored `spec.json` using the signed-in `claude.ai` account, with API-key/base-URL/model environment overrides excluded. The pre-existing third-party API configuration initially rejected the request; no credits were purchased or account configuration changed.

Claude Code had no authenticated Figma MCP connection. `publish.js` translates its detailed screen specification into genuine editable Figma nodes through Codex's connected Figma tools. The published file contains eight screens, three reusable shell components and 39 color variables. `state.json` records the original created node IDs. Later targeted refinements added the wizard action footer (`3:520`) and corrected navigation selection and the administrator badge width.

The implementation follows Figma's returned design context, preserving Inter, the palette, 224px resource sidebar, 56px header, dense table/hardware layouts, modal structure and persistent task dock. Sample resources and account labels are replaced by live inventory and local account data. Proposed prototype features outside the requested implementation (PAM realms, migration, snapshots and pool administration) are not presented as working functionality.

| Screen | Figma node |
| --- | --- |
| Login | `2:122` |
| Overview | `2:139` |
| VM inventory | `2:325` |
| VM hardware | `2:485` |
| Create VM | `2:614` |
| Networks | `2:819` |
| ISO library | `2:952` |
| Access control | `2:1076` |

All screen UI is editable text, frames and component instances. No flattened screen images are embedded. Font family was explicitly checked as Inter, and returned Figma composition screenshots were inspected before implementation.
