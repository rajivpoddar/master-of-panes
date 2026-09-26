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
