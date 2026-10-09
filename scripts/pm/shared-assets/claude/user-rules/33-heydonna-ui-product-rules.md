# HeyDonna UI Product Rules

Product-level rules for every HeyDonna planner, implementer, and reviewer.
Source: Rajiv, #heydonna-dev (C0ALZJHGE49) ts 1790433311.413529.

## No disabled precondition buttons

UI RULE (Rajiv 2026-09-26): Do not gate actions with disabled buttons. Buttons stay enabled; on click, validate and render an error state on the offending control (checkbox/field error styling + inline message) and do not proceed. The only allowed disabled state is the action's own in-flight/double-submit guard. Plans and reviews must call out any new `disabled=` on a button that encodes a precondition and require the error-state pattern instead.

Violation:

```tsx
<Button disabled={!agreed} onClick={submit}>Continue</Button>
```

Required pattern:

```tsx
<Checkbox checked={agreed} aria-invalid={showError && !agreed} />
{showError && !agreed && <p role="alert">Please confirm to continue.</p>}
<Button
  onClick={() => {
    if (!agreed) { setShowError(true); return; }
    submit();
  }}
  disabled={isSubmitting}
>
  Continue
</Button>
```

Allowed: `disabled={isSubmitting}` / `disabled={isPending}` guarding the
button's own in-flight request. Not allowed: `disabled` driven by form
validity, missing selections, unchecked consent, or any other precondition.
Tests must click the enabled button with the precondition unmet and assert
the error state renders and the action does not run.

## Menu-action errors go in the toast only

Rajiv 2026-10-09 (C0ALZJHGE49 thread 1791537587.688969, ts 1791537815.457389): *"why is the error message in the menu item? that's an anti pattern. it should only be in the toast"*.

For menu / menubar / context-menu actions, a refused action shows a toast ONLY. Never render inline error text inside the menu item, and don't hold the menu open for an error. The item stays enabled (the no-disabled-precondition rule still applies). The inline field-error pattern above is for form controls, not menu items.

Violation:

```tsx
<MenuItem onSelect={(e)=>{ if(!ok){ e.preventDefault(); setErr(msg); return; } run(); }}>
  Remove Errata section {err && <span role="alert">{err}</span>}
</MenuItem>
```

Required pattern:

```tsx
<MenuItem onSelect={()=>{ if(!ok){ toast({ title: msg, variant: "destructive" }); return; } run(); }}>
  Remove Errata section
</MenuItem>
```

Tests: click the enabled item with the precondition unmet; assert the toast shows, no `role="alert"`/`aria-invalid` renders inside the menu, and the action does not run.
