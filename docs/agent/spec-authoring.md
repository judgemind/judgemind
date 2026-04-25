# Spec Authoring

How to write and restructure architecture specs and long-lived design docs in Judgemind. Consult this before drafting any new spec or significantly reorganizing an existing one.

## Today vs. Direction Split

Every architecture or product spec separates **Today** (implemented and running) from **Direction** (aspirational, not yet built). Readers must never have to guess whether a component, API, schema, or feature actually exists.

Structure every new spec as:

```
# 1. Principles           (cross-cutting, stable)
# 2. System Overview      (describes current reality, not aspiration)
# 3. Today                (everything below here is implemented and running)
#     3.x subsystems...
# 4. Direction            (everything below here is not yet built)
#     4.x planned items...
```

## Rules

- **Don't mix.** A Today section describes only what exists. A Direction section describes only what doesn't. No "partially implemented" hedge prose — if it's partial, name the shipped part in Today and the unbuilt part in Direction.
- **Speculative ideas** go in Direction or a separate roadmap doc. Never in Today.
- **Principles and cross-cutting constraints** stay in §1.

## Reference Patterns

`docs/specs/architecture-spec-v1.md` and `docs/data-flow.md` are the canonical examples of this structure in the Judgemind codebase.
