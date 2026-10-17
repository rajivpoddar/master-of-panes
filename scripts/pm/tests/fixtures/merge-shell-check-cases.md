# Existing Visual Verdict: SHELL_CHECK Cases

Instruction-only decision fixtures for the single independent candidate review.
These are synthetic evidence descriptions, not screenshots or app QA results.
Use the candidate's section 4; no browser, paid tests, app mutation or new gate.

| Case | Existing evidence | Expected visual decision |
| --- | --- | --- |
| Canonical dashboard | Exact-head contextual screenshot has Donna logo, DashboardSideBar and DashboardTopNav; unchanged applicable dashboard layout and sibling at matching desktop viewport/state agree. | SHELL_CHECK passes in the existing visual verdict. |
| Missing shell | Members-page content matches mock, but contextual screenshot replaces dashboard navigation with plain HeyDonna text; no layout exception. | Block for concrete missing navigation/branding integration, not content acceptance. |
| Cropped component | Only a correct cropped modal is shown; route identity says dashboard, but no contextual image or applicable sibling reference exists. | Return exactly the missing page context/reference to PM/UI owner; crop alone cannot pass shell. Do not recapture here. |
| Content approval is not waiver | Approved inner-content mock omits chrome; actual dashboard page omits logo/sidebar/nav. | Block; neither omitted mock chrome nor content approval explicitly approves a different page layout. |
| Actual alternate approval | Contextual screenshot matches an intentionally distinct layout; recorded approval explicitly covers this page/layout change and exact authored correspondence is established. | SHELL_CHECK passes using the verified applicable exception; do not invent a dashboard requirement. |
| Responsive dashboard | Mobile screenshot matches dashboard sibling at the same viewport/state; real branding/top navigation and responsive collapsed sidebar behavior agree. | SHELL_CHECK passes; collapse is not absence. No demand to repeat chrome in every modal image. |
| Auth layout | Sign-in screenshot matches established auth layout and sibling/state, with no dashboard sidebar by design. | Use auth shell; do not force dashboard chrome. |
| Editor layout | Editor screenshot matches established distinct editor layout and applicable sibling/state. | Use editor shell, preserving existing visual proof; do not force dashboard chrome. |
| Non-app viewers | Marketing/blog/docs/benchmark-viewer-only scope in supported exemption lane. | No in-app dashboard SHELL_CHECK; preserve existing lane, no new image/build/check requirement. |
| No UI change | Backend/test/workflow-only authored change. | Existing VISUAL_NOT_APPLICABLE unchanged; no shell screenshot required. |

In all applicable cases, an already sufficient contextual image and reusable
unchanged layout/sibling reference suffice. Actual image inspection and existing
exact-head correspondence remain required. Record the decision within the
existing visual verdict only. This table does not certify any real PR or grant
an exception to #10210, #10218, or another existing hold.
