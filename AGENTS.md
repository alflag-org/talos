# Talos repository guidance

- Keep public commands under `commands/` and implementation under `src/talos/`.
- Atlas owns execution, runtimes, shims, context, run records, and secret retrieval.
  Use its public `atlas_core` API; do not implement a scheduler or inventory store.
- Commands must be read-only unless mutation is explicitly requested. Registry
  synchronization creates and updates assets only; it never deletes them.
- Match assets by external identity, never by hostname. Preserve unmanaged fields.
- Resolve required secrets before API operations. Never display raw HTTP errors,
  response bodies, credentials, or secret-bearing objects.
- Use synthetic fixtures and local HTTP mocks in tests. Live synchronization is a
  separate operator action and is never a CI or deployment step.
- Use English for code and documentation. Keep documentation current-facing and
  concise; do not commit design histories or temporary progress notes.
- Use `mise run check` before delivery. Keep changes within the selected command.
