# Replay semantics

`recorded` mode loads model and tool results from CAS and performs no external calls. It debugs
downstream orchestration deterministically, but does not claim that a provider would answer the same
way today.

`fresh_model` calls model adapters while replaying tool results and file changes. This isolates
model, prompt, and context-policy changes from tool drift.

`fresh_all` calls both model and tool adapters in a temporary workspace restored from the initial
checkpoint. At each workspace delta it compares actual state with the recorded expected state.
Side effects outside this workspace remain the adapter author's responsibility.

JSON comparison reports exact paths for missing, unexpected, type, and value changes. Policies can
ignore known volatile paths or tolerate bounded numeric differences. Exceptions compare normalized
failure signatures. Workspace manifests compare path, mode, kind, and content digest.

## Reducer correctness

A reducer oracle returns a normalized signature, not a boolean. The baseline is checked for
stability. Each candidate is restored in a new directory and run at least twice by default. Only
the original signature counts as reproduction; a different crash is a non-reproduction.

