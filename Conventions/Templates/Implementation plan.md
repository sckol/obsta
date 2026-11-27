---
Template: Implementation plan
Building block:
Plan type: First implementation, Modification, Refactoring, Bug fix
Status: In progress, Done, Rejected
---
## Context
### Goal
Describe the observable result this plan should deliver.
### Specification and scope
List the permitted files and note sections within the [[Conventions/Разбивка программы на блоки|scope of the selected development type]]. Include affected descendants for first implementation, modification and bug fixing.
### Plan status
Record the current status of the whole plan, use one of: Draft, In progress, Blocked, Interrupted, Done. Draft is the initial value. Draft means the plan is being prepared; In progress means execution has started; Blocked means an obstacle prevents progress; Interrupted means execution stopped before completion; Done means all tasks, required checks and final review have passed.
## *Tasks*
### Execution order and progress
Use the table for sequential tasks, one row per task. Execute rows from top to bottom. Record the concrete change and file paths, a verification command with setup and expected outcome, and the actual progress and result.

| #   | Task / files                             | Coder                      | Verification                            | Progress / result                                            |
| --- | ---------------------------------------- | -------------------------- | --------------------------------------- | ------------------------------------------------------------ |
| 1   | `<concrete change and exact file paths>` | `<coder> or <coder_light>` | `<command, setup and expected outcome>` | Pending, in progress, in review, blocked, interrupted, done. |
Don't use checkboxes in the table, they are reserved for Actions.
### Task details
Add details here only when a table cell is too short: use the task number to identify instructions, test output or review findings.
## *Actions*
Select one action to request execution or continuation of this plan.
- [ ] Execute this plan: read this whole note, resolve Block and its Specification and Acceptance, check the scope, then complete the tasks sequentially with checks and review. Update progress and Status; report blockers.
- [ ] Continue this plan: read this whole note and Block, reconcile recorded progress with the current files and any live executor, then resume the first unfinished task without duplicating work. Update progress and Status; report blockers.
## Outcome
### Final verification
Record final Acceptance commands, actual results and the final review verdict; explicitly identify checks not run. Set Plan Status in the Context session to Done only after all required checks and review pass. The fronmatter status is changed by user.

*~~Run Acceptance, all tests and other mandatory checks within the selected block, including its descendants; a project-wide run outside that block is not required. Record commands, working directories and actual results. Report changed blocks, check results and blockers in the original ticket; a failed or unavailable mandatory check keeps the task incomplete.~~*
### *Interruption and next step*
If execution is blocked or interrupted, record the reason and the next safe step here and set Status to Blocked or Interrupted. Use this record together with task progress when continuing.
