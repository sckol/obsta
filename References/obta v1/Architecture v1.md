
## 1 Introduction and Goals

## 1.1 Requirements Overview

*obsta* supports software development with agents while helping a developer retain control of important decisions and limit accumulated technical debt. It makes requirements, architecture, and documentation the primary place to direct and review the work, with Obsidian as the project workspace. Project structure provides a place for each kind of information, and the method helps keep that information current.

The Review workflow#Review marks|review convention distinguishes material written or approved by the human, agent-generated material awaiting review, and material deliberately delegated to an agent. The developer can therefore decide where close review is needed and see which parts have not yet been accepted. This repository follows the method it develops and serves as a reference implementation.

*Avoid duplicated explanations: each requirement, choice, rule, and implementation fact has a primary home defined by Project structure. Other notes summarize it and link the detail, so a change can be made in one place without leaving contradictory copies.*

## 1.2 Quality Goals

1. *Human review: every important agent-written part can be identified and reviewed by the developer. Review coverage retains the historical page-based baseline. Prose-based coverage is selected but not implemented; identifying important passages and a target percentage remain open.*
2. *Self-application: this repository follows the obsta method it develops and provides a usable reference implementation. Its documentation and code should make deviations visible; a quantitative measure remains open.*
3. *Architecture and documentation quality: the project structure, decisions, and Glossary#Source view|source views help a developer keep the design coherent and current as code changes. A quantitative measure remains open.*

*These goals are ranked in this order. The second goal reflects the project's recursive development approach, recorded in the original project brief; see Glossary#Quality goal|Quality goal.*

*The future `Presentations/` folder is for the reviewable `README.md` Glossary#Projection|projection and outward-facing summaries; Project structure defines its place.*

## 1.3 Stakeholders

*obsta is currently a solo project; its developer is the sole decision maker and reviewer.*

## 2 Architecture Constraints

*The bootstrap must work in Codex and Claude Code for new projects and, where applicable, existing ones.*

*Safety in `Guardrails/` gives the shared safety baseline and links to its executable controls.*

## 3 System Scope and Context

*obsta's boundary includes its method notes, project Glossary#Vault|vault structure, and local review tools. The human provides goals, decisions, and Glossary#Review mark|review marks; an agent reads those notes and returns documentation and code for review. Obsidian edits the shared Markdown files, while Git records their history. The technical interface is the repository filesystem; this bootstrap has no service API.*

*arc42 template supplies context guidance; obsta reference summarizes the method. Both belong in References.*

## 4 Solution Strategy

*Start work in the folder matching the first artifact. Use `Decisions/` for early alternatives and accepted choices, and `Scenarios/` for user stories; see Conventions/Project Structure/Decisions.*

The intended way to build with the skill is:

1. *Work with an agent through the Obsidian vault, using Review workflow|the review procedure and `python3 -m review.status --details` to find pages and candidate passages. Review workflow#Review marks|Review marks show prose awaiting attention; wording acceptance is recorded directly in the text. The local checker is read-only, while Obsidian remains the editor.*
2. Offer a reusable Project structure for documents and code. It states where each kind of information belongs and helps the human and agent apply the chosen architecture patterns.
3. *The following steps mainly concern new projects. Bring selected code, tests, and configuration into the vault as readable Review workflow#Level 2 projections|projections so discrepancies with implementation can be checked. Organize code into Glossary#Vertical slice|vertical slices whose purpose and boundaries are clear. Keep each slice's interfaces, frontend, backend, and tests close together; the bootstrap keeps its tests beside its scripts.*
4. *Maintain links among goals, decisions, source notes, and tests so a reader can follow dependencies and assess graph-based quality measures. Report evidence in Reports, starting with Review coverage|the historical review baseline. The `sources` links show examples of code covered by documentation; a project-wide coverage measure still needs a defined scope and denominator.*
5. *Follow Implement a feature in two passes: the agent builds and discards one trial, revises the design and plan from its findings, and implements the lasting version after human review of the revised documentation.*

## 5 Sources (Building Block View)

*Conventions/Project Structure/Sources requires automatic views, maintained manually here. Source projections connects notes and files. Review renders outbound text through Template - Generated Markdown; inward synchronization, Python generation and test execution remain proposed.*

*Follow Sources/Sources order:*

1. *Review slice covers status and Watch feedback for stable new comments and rewrite requests: background polling uses no model; repair coordinators run serially, and foreground work pauses the service. document_checks.py prepares checks; document_links.py resolves internal links.*
2. *build_agents.py selects the three instruction notes. generated_markdown.py reads their `source` and `template`, expands includes, removes Glossary#Frontmatter|frontmatter and review marks, converts links and writes the configured outputs. test_build_agents.py checks rendering and mapping boundaries; test_generated_markdown.py checks the explicit-note CLI.*
3. *Test contracts gives the executable example and test references. check_markdown_examples.py runs the paired example through `render()`; check_test_contracts.py compares signatures with Python definitions. Their tests are test_markdown_examples.py and test_test_contracts.py.*
4. *Template catalog is the `templates/` capability. collect.py discovers Glossary#Template|template definitions throughout the vault, validates name selectors and generates References/Templates/Templates. test_collect.py checks discovery, catalog updates and failure behavior.*
5. *Agent skills sources collects document context through context.py and publishes procedure projections through build.py; test_context.py and test_build.py check their behavior.*
6. *Inception source describes `inception/template.md`, separately from the Review and Template catalog capabilities. Project structure defines slice placement and `sources` provenance.*

## 6 Scenarios (Runtime View)

*Read Develop a feature for the first obsta use case: the human and agents develop a goal from its relevant starting document into a design and a small linked plan. Implement a feature in two passes describes the disposable trial and lasting implementation. Conventions/Project Structure/Scenarios gives the format adapted from arc42 Runtime View. Test contracts separately describes the current executable projection example. Synchronize source projections describes the proposed template-driven interaction and acceptance cases.*

## 7 Configs (Deployment View)

*The Configs folder explains generated AGENTS.md, AGENTS_MIN.md and CLAUDE.md, the user-level Codex config.toml settings and local Claude settings.json. Safety defines agent behavior.*

## 8 Conventions (Crosscutting Concepts)

*The Conventions/Conventions|Conventions index links placement, review, writing and template rules. Agent skills routes documentation work; Document context prepares fresh review. Source projections defines file connections, Template - Generated Markdown covers outbound text and Source projection synchronization specifies reverse operations. Document checks defines inspections; Check document selects them.*

## 9 Decisions (Architecture Decisions)

*The Decisions folder holds early alternatives, accepted choices, their trade-offs, and local Open questions. Conventions/Project Structure/Decisions explains Glossary#ADR|ADR format and how to resolve questions. Use Review workflow, Rules#Test documentation (draft) and Project structure for current rules without reading decision history. Other Architecture hypotheses|candidates remain hypotheses.*

## 10 Reports (Quality Requirements)

*The Reports folder holds dated evidence for quality goals, including measurements and analyzed user feedback that can inform decisions. Review coverage records the first baseline, including counts, scope, and limitations of the page-level percentage. Future reports can record trends, verification outcomes, and gaps; keep executable tests beside source slices and link their results here when useful.*

## 11 TODOs (Risks and Technical Debt)

*The TODOs folder holds Roadmap|ongoing work, persistent risks, and technical debt. Conventions/Project Structure/TODOs distinguishes actions and problems from unresolved choices, which stay locally in their ADRs. TODOs may link those ADRs when further work needs their reasoning.*

## 12 Glossary

*Root Glossary defines terms and links detail or evidence. Feature notes in `Decisions/` can propose names; settled definitions belong in Glossary.*
