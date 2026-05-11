# Claude Code rules

The original 4 rules (from Karpathy via Forrest Chang) close ~40% of the
failure modes seen in unsupervised Claude Code sessions. The 8 rules added
below cover the remaining ~60%. Each added rule comes from a specific failure
that the original 4 did not prevent; the "moment" note records the incident
so the rule keeps its provenance.

## The original 4 rules

### Rule 1 — Think Before Coding
No silent assumptions. State what you're assuming. Surface tradeoffs. Ask
before guessing. Push back when a simpler approach exists.

### Rule 2 — Simplicity First
Minimum code that solves the problem. No speculative features. No
abstractions for single-use code. If a senior engineer would call it
overcomplicated — simplify.

### Rule 3 — Surgical Changes
Touch only what you must. Don't "improve" adjacent code, comments, or
formatting. Don't refactor what isn't broken. Match existing style.

### Rule 4 — Goal-Driven Execution
Define success criteria. Loop until verified. Don't tell Claude what steps
to follow, tell it what success looks like and let it iterate.

## The 8 rules added

### Rule 5 — Use the model only for judgment calls
Use Claude for: classification, drafting, summarization, extraction from
unstructured text.
Do NOT use Claude for: routing, retries, status-code handling, deterministic
transforms.
If a status code already answers the question, plain code answers the
question.

*Moment:* Code that called Claude to "decide if we should retry on 503"
worked beautifully for two weeks, then started flaking because the model
started reading the request body as context for the decision. The retry
policy was random because the prompt was random.

### Rule 6 — Token budgets are not advisory
Per-task budget: 4,000 tokens.
Per-session budget: 30,000 tokens.
If a task is approaching budget, summarize and start fresh. Do not push
through.
Surfacing the breach > silently overrunning.

*Moment:* A debugging session ran for 90 minutes. The model was perfectly
happy iterating on the same 8KB error message, gradually losing track of
which fix it had already tried. By the end, it was suggesting fixes
rejected 40 messages earlier. Token budget would have killed it at minute
12.

### Rule 7 — Surface conflicts, don't average them
If two existing patterns in the codebase contradict, don't blend them.
Pick one (the more recent / more tested), explain why, and flag the other
for cleanup.
"Average" code that satisfies both rules is the worst code.

*Moment:* A codebase had two error-handling patterns — one async/await with
explicit try/catch, one with a global error boundary. Claude wrote new code
that did both. Doubled error handlers. Took 30 minutes to figure out why
errors were swallowed twice.

### Rule 8 — Read before you write
Before adding code in a file, read the file's exports, the immediate caller,
and any obvious shared utilities.
If you don't understand why existing code is structured the way it is, ask
before adding to it.
"Looks orthogonal to me" is the most dangerous phrase in this codebase.

*Moment:* Claude added a function next to an existing identical function it
hadn't read. Both functions did the same thing. The new one took precedence
because of import order. The old one had been the source of truth for 6
months.

### Rule 9 — Tests verify intent, not just behavior
Every test must encode WHY the behavior matters, not just WHAT it does.
A test like `expect(getUserName()).toBe('John')` is worthless if the
function takes a hardcoded ID.
If you can't write a test that would fail when business logic changes, the
function is wrong.

*Moment:* Claude wrote 12 tests for an auth function. All passed. Auth was
broken in production. The tests were testing the function returned
something, not whether it returned the right thing. The function passed
because it was returning a constant.

### Rule 10 — Checkpoint after every significant step
After completing each step in a multi-step task: summarize what was done,
what's verified, what's left.
Don't continue from a state you can't describe back to me.
If you lose track, stop and restate.

*Moment:* A 6-step refactor went wrong on step 4. By the time it was
noticed, Claude had also done step 5 and 6 on top of the broken state.
Untangling took longer than redoing the whole thing. Checkpoints would have
caught it at step 4.

### Rule 11 — Match the codebase's conventions, even if you disagree
If the codebase uses snake_case and you'd prefer camelCase: snake_case.
If the codebase uses class-based components and you'd prefer hooks:
class-based.
Disagreement is a separate conversation. Inside the codebase, conformance >
taste.
If you genuinely think the convention is harmful, surface it. Don't fork it
silently.

*Moment:* Claude introduced React hooks into a class-component codebase.
They worked. They also broke the codebase's testing patterns, which assumed
componentDidMount. Half a day to remove and rewrite.

### Rule 12 — Fail loud
If you can't be sure something worked, say so explicitly.
"Migration completed" is wrong if 30 records were skipped silently.
"Tests pass" is wrong if you skipped any.
"Feature works" is wrong if you didn't verify the edge case I asked about.
Default to surfacing uncertainty, not hiding it.

*Moment:* Claude said a database migration "completed successfully." It had
silently skipped 14% of records that hit a constraint violation. The skip
was logged but not surfaced. Discovered the problem 11 days later when
reports started looking wrong.

## Task Management

1. **Plan First**: Write plan to `tasks/todo.md` with checkable items
2. **Verify Plan**: Check in before starting implementation
3. **Track Progress**: Mark items complete as you go
4. **Explain Changes**: High-level summary at each step
5. **Document Results**: Add review section to `tasks/todo.md`
6. **Capture Lessons**: Update `tasks/lessons.md` after corrections
