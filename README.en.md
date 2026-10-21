# ToneRelay — Adobe Lightroom Classic MCP

[中文](README.md) · [English](README.en.md)

### Describe the look you want, and refine your photos with an AI agent.

> Source preview: source code and installation instructions are available, but no stable installer has been released and full cross-platform acceptance has not been declared. Pin a commit when installing, and verify your actual environment using that version's installation instructions.

A local Model Context Protocol (MCP) server for Adobe Lightroom Classic: conversational AI photo editing, reference-based color matching and color grading, subject masks, photo previews, and batch photo culling through an AI agent. View your selected photo and folder previews, refine people and lighting, then keep discussing the result. Edits remain in Lightroom Classic.

## [Open ToneRelay Portal · Explore features, real editing results, and installation guidance →](https://reefruan.github.io/tonerelay-portal/?lang=en#experience)

<a href="https://reefruan.github.io/tonerelay-portal/?lang=en#experience"><img src="assets/readme/portal-experience.jpg" alt="Real editing examples on the ToneRelay Portal experience page; click for features, workflows, and installation guidance" width="960"></a>

**[Portal source](https://github.com/ReefRuan/tonerelay-portal) · [One-click installation](#one-click-installation)**

Portal brings together conversational editing, reference-based color matching, subject and lighting adjustments, and style references. Open it through the heading or screenshot.

## See a first version, then keep refining

> “The shadows feel too dark. Adjust them a little more.”

Discuss the look you want, or share a reference image. After the agent produces a first version, continue refining shadows, highlights, warmth, and depth. The following example shows a real workflow with further refinement based on feedback in the same conversation.

[![Conversational refinement in Adobe Lightroom Classic with ToneRelay MCP: reference image, first result, and a second version with brighter shadows](assets/readme/dialogue-workflow.png)](assets/readme/dialogue-workflow.png)

The left shows Lightroom itself; the right shows the reference, two rounds of conversation, and their results. Click to enlarge.

## From one photo to a whole set

[![ToneRelay MCP tool directory: Develop settings and readback, batch operations and export, image measurement and comparison](assets/readme/mcp-tools-list.en.jpg)](assets/readme/mcp-tools-list.en.jpg)

| What you want to do | What you can say |
| --- | --- |
| View the selected photo and nearby images | “Show my currently selected photo, then show 10 previews from the same folder.” |
| Match color and tone to a reference | “Use this reference's color temperature and tonal depth to try a first version on the current photo.” |
| Refine a person and the background | “Keep the background's current feel, and refine the subject's exposure and shadows.” |
| Cull and organize photos | “Review this set and suggest ratings and flags first. Write them back only after I confirm.” |
| Explore styles and effects | “Compare different style references and discuss which one suits this photo.” |

### Cull in the background, mark photos your way

ToneRelay provides tools for browsing, comparing, and marking photos in batches while Lightroom runs in the background. You choose the culling criteria; the agent can use Lightroom Classic's native ratings, flags, and color labels to record the results.

[![Lightroom batch culling and organization: folder grid, two-photo comparison, and the agent's rating and flag writeback records](assets/readme/batch-workflow.png)](assets/readme/batch-workflow.png)

### Balance subject exposure with overall lighting

> “Use a mask to restore the subject's exposure, and adjust the overall exposure too.”

Combine global tone changes with local subject adjustments. Review the actual image, then refine brightness, edges, and the balance of the scene.

[![Lightroom subject masking and global exposure: a real comparison of the original photo and the edited image with a brighter subject](assets/readme/subject-mask.png)](assets/readme/subject-mask.png)

### Apply a reference direction across several photos

Lighting and subjects vary from one photo to another. The agent can assess each image against the references, then bring the adjustments back into Lightroom for review.

[![Reference-based Lightroom color workflow: multiple references, choosing a direction for each photo's lighting, and reading back the color-grading result](assets/readme/reference-color.png)](assets/readme/reference-color.png)

These presentation images come from Portal; trimming and rounded corners are for page layout only. The Lightroom and agent conversations within them come from the author's earlier real workflows; photo editing was not rerun for the capture. No photos were redrawn, conversations altered, or results fabricated. They are not speed or token-use benchmarks. [View the original material sources](https://github.com/ReefRuan/tonerelay-portal/blob/main/public/experience/SOURCES.md). Results depend on the photo, reference target, and model used. Screenshots retain their original interface and conversation language.

## One-click installation

Copy this prompt into the current Agent. The two URLs are the official primary and fallback for the same project. The Agent checks existing installations first, then reads the complete instructions in this repository. “One-click” refers to copying; system confirmations and Catalog authorization still require the user.

```text
Please install or connect ToneRelay for Lightroom Classic in the current Agent client.
Use either official repository:
https://github.com/ReefRuan/tonerelay-lightroom-mcp
https://gitee.com/ReefRuan/tonerelay-lightroom-mcp

Check existing installations read-only first. If healthy and compatible, connect only this client; do not reinstall or incidentally upgrade shared components.
Read the selected repository, record its full commit, and read INSTALL-PROMPT.en.md, FAQ.en.md, and INSTALL.en.md in its root completely before following them. If source needs downloading, pin that commit and obtain the complete source under the dedicated ToneRelay installation directory; do not mix component versions.
Preserve photos, Catalogs, and other client configuration. Complete the required authorization and read-only acceptance checks in a real new client session; report anything that remains unverified.
```

The complete context is in this repository: [installation prompt](INSTALL-PROMPT.en.md) · [FAQ](FAQ.en.md) · [platform installation guide](INSTALL.en.md). The prompt and FAQ are synchronized from Portal's public documents; their [source and file hashes](INSTALL-CONTEXT.json) are stored with the source. Once downloaded, they can be read locally without reaching Portal. If GitHub is unavailable, use the official Gitee mirror above, verify the pinned commit and document availability, and do not call a lagging mirror the latest version.

After installation and acceptance checks, open Lightroom Classic and the target Catalog, select a photo, and say:

> Start editing in Lightroom.

## Photos and edits

Normal Develop workflows preserve the original photo through separate virtual copies. You can review existing versions and continue refining them. Ratings, flags, and color labels are written to the Catalog for the confirmed task. Direct file operations, such as RAW sidecar changes, require explicit authorization and specified targets. Use Lightroom's actual readback and returned images as evidence of execution.

ToneRelay is an independent project, not an official Adobe product, and is not affiliated with or endorsed by Adobe. This integration targets **Lightroom Classic**; it does not imply support for cloud-based Lightroom or Photoshop.

## License

Copyright (C) 2026 Reef Ruan.

The author's original code, runtime prompts, Skills, and documentation in the current version of this repository are licensed under [GNU AGPL-3.0-only](LICENSE), matching Portal. Third-party dependencies retain their respective licenses; this project does not license Adobe software, products, or trademarks. Photos, reference images, and screenshot materials are not covered by the code's AGPL grant; their [sources and original licenses](https://github.com/ReefRuan/tonerelay-portal/blob/main/public/experience/SOURCES.md) apply. Historical commits retain the licenses distributed with them; Git history has not been rewritten.

## Contact

Author: [Reef](https://github.com/ReefRuan)

QQ: 1843727310 · Email: [leningruan1@gmail.com](mailto:leningruan1@gmail.com)
