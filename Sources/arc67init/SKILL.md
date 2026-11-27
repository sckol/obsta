---
name: arc67init
description: Initialize the arc67 folder structure for an Obsidian vault, create Architecture.md and the Glossary Base, or refresh standard arc67 templates. Use when the user asks to initialize arc67, initialize its folders, set up arc67 in a vault, or refresh its standard templates.
---

# Initialize arc67

Run `obsta arc67init "/absolute/path/to/vault"` with an explicit path to the requested vault. For an obstagent ticket that does not specify another vault, use the vault root from the execution context.

When working from the obsta source checkout without the installed command, run `PYTHONPATH="Sources/obstagent" python3 -m obsta arc67init "/absolute/path/to/vault"` from the source checkout root.

The command creates missing arc67 folders, `Architecture.md` and `Glossary.base`. It refreshes the standard templates by filename in `Conventions/Templates` and selects that folder in Obsidian. Existing architecture and glossary files and additional templates are preserved. Report the target vault and any error from the command.
