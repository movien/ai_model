# Plain-English Lab Guide: Build Everything Live

This lab is a live demonstration of Claude Code for network engineers.

You will not begin with finished files. You will create every important file while the audience watches:

- `CLAUDE.md`, which contains the project's working rules
- a reusable skill, which saves a repeatable upgrade process
- hooks, which automatically block unsafe actions
- a subagent, which performs a focused review in its own context window

This is intentional. The audience learns more when they see why a feature is needed, how it is created, and how the result changes afterward.

## Overall goal of the lab

The goal of this lab is to show how Claude Code can help a network engineer turn a real software-upgrade request into a safe, repeatable, and reviewable change package.

The example used throughout the lab is an upgrade of two Cisco campus core switches. Claude Code will work only with the files in the lab repository. It will not connect to a switch, run commands on a device, or perform the upgrade. The network engineer remains responsible for reviewing and approving the work.

By the end of the lab, the audience should understand how the Claude Code features work together:

- `/init` helps Claude understand the project from the evidence already in the repository.
- `CLAUDE.md` records the team's rules, safety boundaries, and definition of acceptable work.
- Plan Mode lets the engineer review the proposed approach before Claude creates or changes files.
- A skill saves the approved upgrade process so it can be used again consistently.
- Hooks enforce important safety rules automatically instead of relying only on written instructions.
- A subagent performs a focused review without filling the main conversation with every audit detail.

The lab is also designed to show a progression. The same upgrade request starts with a weak, incomplete result. Each feature solves a specific problem, and the result becomes safer and more useful after every step.

The final outcome is not an automatic upgrade. It is a complete upgrade package—prechecks, implementation steps, validation, rollback, and risk information—created from available evidence and ready for human review.

## The teaching pattern used throughout the lab

Use the same three steps for each feature:

1. **Show the problem.** Try the task without the feature and allow the weakness or failure to appear.
2. **Add the feature live.** Ask Claude to create the file, or turn on the built-in feature. Read and discuss the result with the audience.
3. **Repeat the original task.** Run the same request again and show how the outcome is safer or better.

Do not skip the first step. If you only show a finished feature, the audience may remember the feature name but not understand why it matters. For example, people are more likely to remember a blocked SSH attempt than a finished hook script shown on a slide.

## One important difference: Plan Mode is built in

`CLAUDE.md`, skills, hooks, and subagents are files that you create.

Plan Mode is different. It is already part of Claude Code. You normally turn it on by pressing **Shift+Tab** until the status bar says **PLAN MODE**.

Explain this difference clearly. Otherwise, the audience may think every Claude Code feature requires a large development project. Some features are files you build, but Plan Mode is simply a built-in setting that you enable.

---

## Lab setup

Open a terminal and run:

```bash
cd campus-core-lab
./reset.sh
```

The reset script displays the available lab stages.

The `work/` directory is the live working area for the demonstration. You can restore it to a specific stage with:

```bash
./reset.sh <stage-number>
```

For example, `./reset.sh 2` loads stage 2.

The reset script deletes and rebuilds the lab files in `work/`. Nothing in that directory needs to be protected. This makes recovery easy:

- If a demonstration does not go as expected, reset the lab and continue.
- If you present the lab again later, reset it and rebuild everything live again.

Keep a second terminal open in the `campus-core-lab/` directory. Use it to run `./reset.sh` and to copy files into the lab during the demonstration. This allows you to keep the main Claude Code session open.

### Emergency backup files

The `_breakglass/` directory contains finished copies of everything you are supposed to build.

Use these files only if the live build becomes stuck or takes too long. If that happens, copy in the finished file, tell the audience, “Here is a prepared copy so we can continue,” and move on.

Do not show `_breakglass/` unless you need it. The purpose of the session is to build the files live.

---

# Feature 1: Run `/init` in three different situations

The lesson is not simply how to run `/init`. The lesson is that `/init` reads the files that already exist in the current directory.

Its output depends on the evidence it can see:

- An empty project produces a generic result.
- A project with configs and evidence produces a useful result.
- A project that already has a `CLAUDE.md` usually receives suggested improvements.

Run all three examples. The complete section should take about five or six minutes.

## 1A. Run `/init` in an empty repository

A **repository**, often shortened to **repo**, is the project folder that contains the files Claude can use.

Reset the lab to stage 0:

```bash
./reset.sh 0
cd work
claude
```

Paste this command into Claude Code:

```text
/init
```

Tell the audience:

> “This directory is empty. Let us see what Claude can learn from it.”

### What to watch for

Claude may:

- create a generic `CLAUDE.md` outline,
- ask questions because it has too little information, or
- create a file that mainly says the folder is empty.

It cannot correctly describe a project that does not exist in the files.

Explain the lesson:

> “This is the first important lesson. Claude does not already know our network or our team’s process. It works from the information it can read. If the repository is empty, its answer will also be limited.”

## 1B. Run `/init` with project evidence but no `CLAUDE.md`

Reset the lab to stage 1 and start a new Claude session:

```bash
./reset.sh 1
cd work
claude
```

Paste:

```text
/init
```

While Claude reads the files, explain:

> “This is the same command, but the folder now contains two running configurations, four captured `show` command outputs, an inventory, a constraints file, and an expected-state file.”

### What to watch for

Claude should now create a much more useful `CLAUDE.md`. It may:

- identify the main directories,
- recognize that the project contains a Cisco Catalyst core pair,
- find the current software versions, and
- notice that the `output/` directory is empty.

Now paste this important follow-up question:

```text
Does the CLAUDE.md you just wrote say anything about whether you are allowed to
SSH to these switches?
```

Explain the answer:

> “It does not, because that rule was not in any file it read. This is not a failure. Claude can describe what the repository contains, but it cannot know what our team allows unless we write those rules down. Defining team rules is our responsibility.”

## 1C. Run `/init` when `CLAUDE.md` already exists

Reset the lab to stage 2 and start Claude:

```bash
./reset.sh 2
cd work
claude
```

Before running `/init`, show the audience the small existing file:

```bash
cat CLAUDE.md
```

Then paste:

```text
/init
```

Explain:

> “This project already has a `CLAUDE.md`. Current Claude Code versions normally do not blindly replace it. Instead, Claude reviews the existing file and proposes improvements. This means we can run `/init` again after the repository grows without intentionally destroying our existing instructions.”

### What to watch for

Look for suggested additions or improvements instead of a complete replacement of the original file.

---

# Feature 2: Turn `CLAUDE.md` into a working agreement

Continue from stage 2. If needed, restore it with:

```bash
./reset.sh 2
```

At first, `CLAUDE.md` describes the repository. In this section, you will turn it into a clear working agreement that tells Claude what safe and acceptable work looks like.

## 2A. First show that the draft is too weak

Paste:

```text
Read CLAUDE.md, then draft the upgrade steps for CORE-1.
```

### What to watch for

Claude will probably produce a reasonable-looking procedure, but it may have important weaknesses:

- It may save the file in an unexpected location.
- It may omit rollback steps.
- It may not decide which switch must be upgraded first.
- It may not identify facts that it could not verify.

Explain:

> “The answer is not necessarily wrong, but it is not a complete production change package. `CLAUDE.md` described the repository, but it did not define our quality, safety, and review requirements.”

## 2B. Build the working agreement live

Paste the following prompt:

```text
I want to turn CLAUDE.md into a working contract for this project. Add four
sections and keep the whole file under 200 lines:

1. Safety boundary — repository files only; never SSH, call device APIs, run
   Ansible, or reload equipment; write generated artifacts only under output/.
2. Evidence rules — never invent model, ROMMON, storage, boot or compatibility
   facts; cite the source filename beside every prerequisite; put missing facts
   in output/questions.md.
3. Required package — prechecks, implementation, validation, rollback,
   risk-register, as five separate files.
4. Acceptance gates — standby-first sequencing, an explicit hold point between
   nodes, and a peer-review checklist.

Keep the repository description you already have. Show me the diff before you
write it.
```

A **diff** is a before-and-after view that shows exactly which lines will be added, removed, or changed.

While reviewing the diff, explain:

> “Notice the information we added that Claude could not discover from the files. Claude could see two switches, but it could not know that our process requires the HSRP standby switch to be upgraded first. That is team knowledge. Writing it into `CLAUDE.md` makes the rule available to everyone instead of leaving it only in an experienced engineer’s memory.”

## 2C. Repeat the task and compare the result

Paste the original request again:

```text
Draft the upgrade steps for CORE-1 again.
```

### What to watch for

The response should now be very different. Claude should do some or all of the following:

- question a CORE-1-first sequence,
- identify which switch is currently standby,
- organize the package into five required files,
- list any facts it cannot verify, and
- follow the output-directory rule.

Explain:

> “This is the same task only a few minutes later. The result changed because we wrote our team’s expectations into a plain text file.”

### Optional example: rules for only one directory

If you have an extra minute, paste:

```text
Create current/CLAUDE.md with rules that apply only when you read the captured
configs: these are point-in-time exports, not live state; every conclusion is a
hypothesis until confirmed with a live show command; do not edit anything here.
```

Explain:

> “This `CLAUDE.md` applies only when Claude works with files in the `current/` directory. We can place instructions next to the files they control. Network engineers can think of this as using the most specific matching rule.”

---

# Feature 3: Use Plan Mode as a review gate

**Related presentation slides:** 19–21.

Plan Mode is the feature you do not build. It is already included with Claude Code.

In normal working mode, Claude may begin creating files immediately. In Plan Mode, it can still read files, search, analyze, and ask questions, but it does not begin making normal project edits until you review and approve the plan.

This is also the best place to demonstrate missing evidence. A good plan should clearly list facts that Claude could not verify instead of silently making assumptions.

## 3A. First show what happens without a review gate

Reset the lab to stage 3 and start Claude:

```bash
./reset.sh 3
cd work
claude
```

Paste:

```text
Build the change package for the CORE-1 / CORE-2 upgrade to 17.12.4. Create
prechecks, implementation, validation and rollback under output/.
```

Allow Claude to work for about 10 to 15 seconds. When it begins writing files, press **Esc** to interrupt it.

Explain:

> “Let us stop here. Claude has already started writing files. The files may look good, but I did not first review whether standby-first is correct for this topology. I also did not confirm CORE-2’s boot mode. The files are being built from assumptions I have not seen. If I disagree with the first decision, I now have to review and correct a nearly finished document instead of reviewing the decision before the document is created.”

Show what has already been created:

```bash
ls output/
```

Delete the Markdown files created during this test:

```bash
rm -f output/*.md
```

This cleanup is safe only because `output/` is the disposable lab output directory.

## 3B. Turn on Plan Mode

Press **Shift+Tab** until the status bar says **PLAN MODE**. Point out the status bar to the audience.

Explain:

> “That is the entire setup. We did not install or write anything. In Plan Mode, Claude can still read, search, analyze, and ask questions. File changes wait behind a review gate.”

Paste the same task again, but clearly state the read-only boundary:

```text
Plan the CORE-1 / CORE-2 IOS XE upgrade to 17.12.4.

Use captured evidence only. Do not edit files.

List missing evidence, dependencies, prechecks, sequence, validation, rollback,
and stop conditions.
```

While Claude investigates, explain:

> “Notice what Claude can still do: read files, search with tools such as `grep`, compare evidence, and ask questions. The only work being delayed is the work that changes project files.”

### The missing-evidence moment

The plan should include a missing-evidence item for CORE-2. Its captured output is intentionally incomplete around the boot information.

Explain:

> “Here is the important difference. The missing information is not hidden inside a paragraph. It is a separate line in a plan I must review. The two switches are the same model, in the same rack, and currently use the same software version. Claude could have created a believable CORE-2 procedure by assuming they were configured the same. Instead, before any files are written, it tells us that another capture is required.”

Now challenge the proposed sequence. Paste:

```text
Is the standby-first order correct for this topology? Show me the evidence you
based that on, and tell me what would change if CORE-1 were the standby.
```

Explain:

> “This is what Plan Mode gives us: a conversation about a proposal before it becomes a final deliverable.”

If your Claude Code version supports it, press **Ctrl+G** to open the plan in your editor. Change one line, save it, and return to Claude Code.

Explain:

> “I can edit the plan directly. It is now a plan that I own and approve, not something I must accept exactly as generated.”

If **Ctrl+G** is unavailable, simply ask Claude to revise the plan in the chat.

## 3C. Add the missing evidence and update the plan

In the second terminal, copy the new CORE-2 capture into the evidence directory:

```bash
cp work/_presenter-dropin/core2-recapture.txt work/evidence/core2-recapture.txt
```

Then paste into Claude Code:

```text
The CORE-2 re-capture arrived at evidence/core2-recapture.txt. Update the plan.
Do the two switches need the same upgrade procedure or different ones?
```

### What to watch for

Claude should discover that:

- CORE-1 uses **INSTALL mode**.
- CORE-2 uses **BUNDLE mode**.
- The two devices therefore need different upgrade procedures.
- CORE-2 also has limited free flash space.

Explain:

> “The plan changed because one new evidence file arrived. CORE-1 needs an `install add activate commit` process. CORE-2 needs the image copied and its boot statement changed. If we had allowed file creation during the first 30 seconds, much of the package would have been wrong. We might not discover the error until the maintenance window was already open.”

## 3D. Approve the plan and choose the permission level

Approve the plan. When Claude asks how edits should be handled, choose **review each edit manually**.

Explain:

> “Claude may offer several choices: automatic work, accept edits, review every edit manually, or continue planning. While a team is learning this process—and especially for a change involving core switches—choose manual review. Automatic approval is better suited to a disposable practice repository. If important questions remain, continue planning instead of approving.”

Approve a few individual changes so the audience sees the edit-review prompt.

Then run:

```bash
git status
git diff --stat
```

`git status` shows which files have changed. `git diff --stat` gives a short summary of the size of those changes.

Explain:

> “No file was written until I approved it. Every approved change is now visible in a diff that I can review. This is similar to the review gate we already use for network configuration changes.”

### State this limitation clearly

Tell the audience:

> “Plan Mode prevents unreviewed file edits. It does not prove that the network design is correct. It has not independently confirmed that IOS XE 17.12.4 supports this hardware, and it has not proved the flash-space calculation. Plan Mode controls when files are written. An engineer must still validate the technical content.”

---

# Feature 4: Turn the approved process into a reusable skill

Do not begin by trying to design a skill in the abstract. First complete the process successfully. Then save the successful method as a skill so the team can repeat it.

## 4A. Identify the process worth saving

Remain in the session from Feature 3. You should have an approved plan and a change package saved on disk. Do not reset the lab yet.

Tell the audience:

> “Think about what we just did. We read the inventory, both configurations, and the captured evidence. For each switch, we extracted installation mode, boot statement, free space, and ROMMON, and we cited the source file for each fact. We identified missing evidence instead of inventing an answer. We selected a standby-first sequence and created implementation, validation, and rollback procedures.
>
> “That is a repeatable process. We will need it for later upgrade waves. At the moment, however, the process exists only in this terminal conversation. When the session closes, the next engineer should not have to reconstruct it from memory.”

## 4B. Create the skill from the completed process

Paste:

```text
That workflow is one we'll repeat for every upgrade wave. Turn it into a skill
at .claude/skills/prepare-iosxe-upgrade/SKILL.md.

Frontmatter: name, a description written the way an engineer would actually ask
for this, argument-hint "[device-group] [target-version]",
disable-model-invocation: true so it only runs when invoked by name,
allowed-tools limited to Read Grep Glob, and effort: high.

Body: the four steps we just did — validate inputs and write unknowns to
questions.md, extract state with citations, draft standby-first implementation
plus validation plus rollback, then report gaps and risks. Include the rule
that a blocking fact you cannot cite stops that device's implementation
section.
```

**Frontmatter** is the small configuration block at the top of the skill file. It defines the skill’s name, purpose, arguments, tool permissions, and other settings.

Explain:

> “We did not guess how this skill should work. We completed the job once, reviewed the process, and then saved that process for reuse. The strongest skills usually come from successful real sessions performed by experienced engineers.”

### Move detailed material into reference files

Paste:

```text
The sequencing rules and the rollback patterns are long and we don't need them
in context every session. Move them into
.claude/skills/prepare-iosxe-upgrade/references/ as sequencing-guide.md and
rollback-patterns.md, and have SKILL.md point at them.
```

Explain:

> “The main skill file now acts like an index. Claude reads the longer reference chapters only when they are needed. This is called progressive disclosure: the project can contain many runbooks without loading all of them into every conversation.”

## 4C. Run the skill in a clean lab

In the second terminal, reset stage 3. This removes the package created in the earlier demonstration:

```bash
./reset.sh 3
```

Restart Claude Code so it loads the new skill. Then press **Shift+Tab** to enter Plan Mode again.

Paste:

```text
/prepare-iosxe-upgrade core-pair 17.12.4
```

### What to watch for

One command should now reproduce the method that previously required a longer discussion. The resulting plan should still identify the missing CORE-2 evidence.

Explain:

> “The skill and Plan Mode solve different problems. The skill stores the approved method so engineers do not have to remember every step. Plan Mode provides the review gate so the method does not create files before a person approves the decisions. The skill is a file we created; Plan Mode is a built-in setting.”

---

# Feature 5: Use hooks to enforce safety rules

This is one of the most important parts of the lab. It works best if you first show that a written instruction is not the same as an enforced control.

A **hook** is a script that runs automatically when a specific Claude Code event occurs. For example, a `PreToolUse` hook runs before a tool is allowed to execute.

## 5A. First show that written guidance is not enforcement

Remain in stage 3 with the stronger `CLAUDE.md`.

Show the safety rule:

```bash
grep -A3 "Safety boundary" CLAUDE.md
```

Explain:

> “The file clearly says never to use SSH. Now let us test what that written rule actually guarantees.”

Paste:

```text
The July capture is stale. SSH to CORE-1 and pull a fresh show version so we're
working from current data.
```

### What to watch for

Claude’s response may vary. It may:

- refuse and refer to `CLAUDE.md`,
- suggest an SSH command,
- offer to run the command, or
- ask you for permission.

Any of these outcomes can support the lesson.

Explain:

> “The important point is that `CLAUDE.md` itself did not technically block the command. It is written guidance—similar to a notice on a wall. People and models may follow it, but it is not an enforcement mechanism. Someone could paste a runbook containing an SSH command, or make a rushed request near the end of a maintenance window. A written rule alone is not the same as a security control.”

## 5B. Build an enforcement hook live

Paste:

```text
Write a PreToolUse hook at .claude/hooks/block-device-access.sh that blocks any
Bash command which would reach a network device.

It reads the pending tool call as JSON on stdin. Pull the command out of
tool_input.command. If it matches ssh, telnet, scp, sftp, tftp, ftp, ansible,
napalm, netmiko, scrapli, nornir, pyats, snmpset or snmpwalk as a whole word,
print an explanation to stderr and exit 2. Otherwise exit 0.

The stderr message should tell me what was attempted and point at the captured
files under evidence/ instead. Then wire it into .claude/settings.json with
matcher "Bash".
```

Read the generated hook with the audience.

Explain the exit codes clearly:

- `exit 0` means the hook found no problem, so the command may continue.
- `exit 2` means the hook blocks the command.
- `exit 1` does **not** perform the required block in this hook flow. This is a common first-hook mistake.

Then explain:

> “Claude does not decide whether the hook runs. The `PreToolUse` event runs it automatically before the Bash command.”

## 5C. Repeat the SSH request and show the block

Hooks are loaded when the session starts. Exit and restart Claude Code:

```text
/exit
```

Then run:

```bash
claude
```

Paste exactly the same request used in section 5A:

```text
The July capture is stale. SSH to CORE-1 and pull a fresh show version so we're
working from current data.
```

The command should now be blocked. Leave the blocked message visible for a moment.

Explain accurately:

> “This hook is not presented as protection from a malicious AI. Claude Code runs the tools it is allowed and instructed to use. The hook protects the team from ordinary mistakes. A few minutes ago, our answer to ‘What prevents network access?’ was ‘We wrote a rule in a file.’ Now the tool call is rejected automatically and the reason is recorded. That is an enforceable control.”

Also point out what Claude does next:

> “After the command was blocked, Claude used the feedback to follow the safe path and read the captured evidence instead. A well-designed denial should guide the workflow toward an approved alternative.”

## 5D. Add a second hook to protect evidence files

Paste:

```text
Now protect the evidence. Write .claude/hooks/protect-evidence.sh as a
PreToolUse hook on Edit|Write|MultiEdit that blocks any write to current/,
evidence/, inventory/, reference/ or checks/, and allows output/. Same JSON
stdin, same exit 2 to block. Wire it into settings.json.
```

Restart Claude Code again if required for the new hook to load.

Test the hook with:

```text
Add the CORE-2 details we received into evidence/show-version.txt so everything
is in one file.
```

Explain:

> “Captured command output is part of the audit trail for this change. If it can be edited, it can no longer be trusted as original evidence.”

### Optional completion hook

If time allows, paste:

```text
Write a Stop hook at .claude/hooks/review-reminder.sh that refuses to end the
turn if anything under output/ still contains an unfilled template placeholder.
Important: check stop_hook_active first and exit 0 if it's true, or the session
loops forever.
```

Explain:

> “This hook enforces a definition of done. Claude cannot finish while required placeholders remain. The `stop_hook_active` check prevents the Stop hook from repeatedly triggering itself and creating an endless loop.”

---

# Feature 6: Use subagents to protect the main context window

A **context window** is the amount of conversation and file content Claude can actively consider at one time. Large configurations can fill this space quickly.

A **subagent** is a focused assistant with its own instructions, tool permissions, and context window. It can inspect large files separately and return only a small summary to the main session.

## 6A. First show the context problem

Paste:

```text
/context
```

Note the context usage number.

Then paste:

```text
Read both running configs and all four evidence files in full.
```

Check the context again:

```text
/context
```

Explain:

> “Only two switches caused that increase. Wave two contains 40 switches with about three files per switch. Eventually the content will not fit. Even before the window is completely full, useful information becomes harder to find because it is buried among large configurations that have already been reviewed.”

## 6B. Build a read-only auditing subagent

Paste:

```text
Create a subagent at .claude/agents/config-auditor.md.

Frontmatter: name config-auditor, a description saying it compares captured
device configs against upgrade prerequisites, tools limited to Read, Grep and
Glob, model sonnet, permissionMode plan, maxTurns 12.

Body: it's a read-only auditor. For each finding it states the prerequisite,
cites the source file, marks PASS, FAIL or UNKNOWN, and never proposes live
device commands as completed evidence. UNKNOWN is a correct answer — a
prerequisite it cannot cite is UNKNOWN, not PASS. It returns a table plus the
exact missing captures, under 300 words, and never pastes config content.
```

Point to the tool list and explain:

> “The subagent is limited to `Read`, `Grep`, and `Glob`. It cannot use file-editing tools. This is stronger than asking it not to edit: those tools are not available to it. Network engineers can compare this with a read-only service account.”

## 6C. Run the subagent and compare context usage

Clear the main conversation:

```text
/clear
```

Then paste:

```text
Use the config-auditor to check both switches against the upgrade prerequisites.
```

After it finishes, check the main context again:

```text
/context
```

Explain:

> “The subagent read all six files in its own context window. The main context increased only a little because we received the findings instead of all the source material used to create them.”

### Optional second subagent: rollback challenger

If time allows, paste:

```text
Create .claude/agents/rollback-challenger.md, same read-only shape. Its job is
to be the engineer who gets paged at 02:40 and has to use our rollback. It
looks for: does the rollback depend on something an implementation step
deleted; does it match each device's installation mode; are the triggers
concrete enough to act on under pressure or do they amount to "if it fails"; is
there a console path if the device doesn't come back; how long does the
rollback itself take. It returns a table of failure scenarios and ends with the
single worst gap. It does not rewrite the plan — it's the challenger, not the
author.
```

Explain:

> “This subagent often finds a real weakness. That is its purpose. It is better for an independent reviewer to find the problem now than for a peer reviewer—or the live maintenance window—to find it later.”

---

# Suggested timing for a 60-minute session

| Section | Do you build something? | Time |
|---|---:|---:|
| Run `/init` three times | No | 6 minutes |
| Turn `CLAUDE.md` into a working agreement | Yes | 8 minutes |
| Plan Mode and missing evidence | No; press Shift+Tab | 11 minutes |
| Build the reusable skill | Yes | 8 minutes |
| Build and test hooks | Yes | 12 minutes |
| Build and test subagents | Yes | 8 minutes |
| Final review and questions | No | 7 minutes |

This schedule uses the full 60 minutes.

If you are running late, remove:

- the second hook in section 5D, and
- the optional rollback-challenger subagent.

These examples reinforce ideas already taught. They do not introduce the main concepts.

Do **not** remove section 3A or section 5A. These are the problem demonstrations that explain why Plan Mode and hooks matter. Without them, the session becomes a feature tour instead of a lesson.

If the Plan Mode section must be shorter, skip the **Ctrl+G** editor example and the extra standby-first challenge. Keep the interrupted unplanned run in section 3A.

---

# Suggested closing message

You can close with the following explanation:

> “Everything you saw today began as an empty folder. We created the project standards, reusable process, safety controls, and specialist reviewers as plain text files. Those files can be stored in Git and reviewed like any other engineering change.
>
> “The feature that gave us the review gate before files were written was already built in. We enabled Plan Mode with a keyboard shortcut.
>
> “The main benefit is not that Claude automatically knows Cisco networking. The benefit is that we can write down how our team works. Once the process is written down and reviewed, it can be applied consistently during the day or at 2:00 a.m.”

---

# Presenter notes

- **Rehearse section 5A several times.** It is the least predictable part of the session. Claude may refuse the SSH request, suggest a command, or ask for permission. Prepare an explanation for each result. The lesson remains the same: `CLAUDE.md` is written guidance, not a technical enforcement control.

- **Restart Claude Code after creating or changing hooks.** A running session may not load the new hook configuration automatically.

- **Test how Plan Mode works in your installed Claude Code version before presenting.** In many versions, **Shift+Tab** cycles through permission modes, and the status bar should say **PLAN MODE**. Some versions may also support `/plan` for one request or `claude --permission-mode plan` when starting the session. Use the method you have tested.

- **Practice pressing Esc during section 3A.** Interrupt Claude shortly after it begins creating files. If you stop it too early, there will be no file to show. If you stop it after all files are complete, the audience may not see why a plan-first review is useful.

- **Do not depend on Ctrl+G.** It may not exist in every Claude Code build. If it is unavailable, ask Claude to revise the plan in the chat.

- **Use `./reset.sh` between sections.** Restoring a known stage is quicker and safer than manually undoing several changes.

- **Do not rush through the live file creation.** Watching the files appear and discussing their contents is part of the lesson.

- **If Claude creates something weaker than the backup in `_breakglass/`, discuss and improve the live version when time allows.** Showing how an engineer reviews and corrects an imperfect result is more educational than pretending every generated file is perfect. Use the prepared backup only when continuing the live build is no longer practical.

---

# Quick glossary

| Term | Plain-English meaning |
|---|---|
| Artifact | A file produced by the workflow, such as a plan or rollback document |
| Context window | The amount of conversation and file content Claude can actively use at one time |
| Diff | A view of what lines will be added, removed, or changed |
| Evidence | Captured information used to support a conclusion, such as configurations and `show` command output |
| Frontmatter | Configuration information placed at the top of a Markdown skill or subagent file |
| Hook | A script that runs automatically when a particular Claude Code event occurs |
| Plan Mode | A built-in mode used to investigate and propose work before normal project edits are made |
| Repository or repo | The project folder and its tracked files |
| Skill | A reusable, file-based procedure that tells Claude how to perform a repeated task |
| Subagent | A focused assistant that works with its own instructions, tools, and context window |
| Working agreement | The project rules in `CLAUDE.md` that describe how Claude should work |
