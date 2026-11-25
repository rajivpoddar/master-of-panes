# HeyDonna UI Product Rules
Pre-trim: `~/.claude/archive/33-heydonna-ui-product-rules-2026-11-25-pre-trim.md`.

Source: Rajiv, #heydonna-dev (C0ALZJHGE49) ts 1790433311.413529.

## No disabled precondition buttons

UI RULE (Rajiv 2026-09-26): Do not gate actions with disabled buttons. Buttons stay enabled; on click, validate and render error state on offending control (checkbox/field error styling + inline message) and don't proceed. Only allowed disabled state is action's own in-flight/double-submit guard. Plans and reviews must call out any new `disabled=` on button that encodes precondition and require error-state pattern instead.

Allowed: `disabled={isSubmitting}` / `disabled={isPending}` guarding button's own in-flight request. Not allowed: `disabled` driven by form validity, missing selections, unchecked consent, or any other precondition. Tests click enabled button with precondition unmet and assert error state renders and action doesn't run.

## Menu-action errors go in the toast only

Rajiv 2026-10-09 (C0ALZJHGE49 thread 1791537587.688969, ts 1791537815.457389).

Rajiv 2026-10-24 (DM D0AMF0XE6TS thread 1792847569.139939).

For menu / menubar / context-menu / dropdown / popover / floating or bubble toolbar / toolbar-button / command-palette actions, refused action shows toast ONLY. Never render inline error text inside menu item, and don't hold menu open for error. Item stays enabled (no-disabled-precondition rule still applies). Inline field-error pattern above is for form controls, not menu items.

Tests: click enabled item with precondition unmet; assert toast shows, no `role="alert"`/`aria-invalid` renders inside menu, and action doesn't run.
