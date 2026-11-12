---
Template: Implementation plan
Status: In progress, Done, Rejected
Plan type: First implementation, Modification, Refactoring, Bug fix
---
## *Context*
### *Goal*
*Describe the observable result this plan should deliver.*

### *Specification and scope*

*Link to the relevant Specification and Acceptance sections of the module note. List the files and note sections this plan may change.*

### *Properties and placement*

*Set Module to a quoted vault link to the block note, for example "[[Sources/theseus]]". Save as Plans/YYYY-MM-DD-kebab-case-slug.md with a unique descriptive slug. Placement and lifecycle: [[Набор скиллов Тесея#План реализации]].*

### *Plan status*

*Status describes the whole plan: Draft, In progress, Blocked, Interrupted, Done. Store one value in frontmatter; Draft is the initial value. Draft means the plan is being prepared; In progress means execution has started; Blocked means an obstacle prevents progress; Interrupted means execution stopped before completion; Done means all tasks, required checks and final review have passed.*

## *Tasks*

### *Execution order and progress*

*Use the table for sequential tasks, one row per task. Execute rows from top to bottom. Record the concrete change and file paths, a verification command with setup and expected outcome, and the actual progress and result.*

| *#* | *Task / files*                             | *Verification*                            | *Progress / result* |
| ------- | ---------------------------------------------- | --------------------------------------------- | ----------------------- |
| *1* | *`<concrete change and exact file paths>`* | *`<command, setup and expected outcome>`* | *pending; not run*  |

*Task states: pending, in progress, in review, blocked, interrupted, done. Mark done after required checks and review pass. Use plain status text in the table; checkboxes are reserved for Actions.*

### *Task details*

*Add details here only when a table cell is too short: use the task number to identify instructions, test output or review findings. For a file block, keep code and tests within its permitted file. Apply TDD and review according to [[Набор скиллов Тесея]] without repeating the process in every row.*

## ~~*Actions*~~

~~*Select one action to request execution or continuation of this plan.*~~

- [ ] *Execute this plan: read this whole note, resolve Module and its Specification and Acceptance, check the scope, then complete the tasks sequentially with checks and review. Update progress and Status; report blockers.*
- [ ] *Continue this plan: read this whole note and Module, reconcile recorded progress with the current files and any live executor, then resume the first unfinished task without duplicating work. Update progress and Status; report blockers.*

## *Outcome*

### *Final verification*

*Record final Acceptance commands, actual results and the final review verdict; explicitly identify checks not run. Set Status to Done only after all required checks and review pass. Record unresolved problems in the module note.*

### *Interruption and next step*

*If execution is blocked or interrupted, record the reason and the next safe step here and set Status to Blocked or Interrupted. Use this record together with task progress when continuing.*
