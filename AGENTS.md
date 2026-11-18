# Repository guidance

- Keep the identity of a session bound to the node instance, engine, and full session ID. Maintain quote and pending-question routing through that identity.
- Keep app credentials, pairing material, SSH keys, transcripts, and runtime databases in the private state directory. Use synthetic fixtures and placeholder configuration in the repository.
- Preserve the distinction between queued, submitted, and completed work. A lost receipt may represent a successful write.
- Use the existing test suite and Ruff. Validate node changes on Windows and against the supported Linux node environment.
