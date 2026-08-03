# Lab Guide: Plain English Explanation

## The Main Idea of the Demonstration
This document is a guide for running a live teaching demonstration. The core philosophy is that you shouldn't prepare everything ahead of time; instead, you build the tools live while the audience watches. 

For almost every feature you show, you will follow a simple three-step pattern:
1. **Show the problem:** Ask the AI to do a task without the proper tools or rules in place, and let the audience watch it make a mistake or do it poorly.
2. **Fix it live:** Turn on a feature or write a rule together with the audience to solve the problem.
3. **Try again and succeed:** Ask the AI to do the exact same task again, and watch it succeed because of the fix you just applied.

It is highly recommended to explain to your audience that while you have to manually write files to create rules and tools, a feature called "Plan Mode" is already built-in and only takes a single keystroke (Shift+Tab) to turn on. This keeps the audience from feeling like the setup is too much work.

## Setup Instructions
*   You will work inside a live folder called `work/`.
*   You can wipe this folder clean and load different stages of the presentation by typing `./reset.sh` and a number.
*   You should keep a backup folder called `_breakglass/` that contains the finished versions of everything you plan to build.
*   If you get stuck or something breaks during the live demo, you can easily copy the finished files from the backup folder to keep the presentation moving.

## Feature 1: The `/init` Command
This section shows how the AI learns about your project.
*   The AI can only understand what it is able to read.
*   If you run the `/init` command in an empty folder, the AI won't be able to give you a meaningful answer.
*   If you run it in a folder with data (like network configurations) but no instruction files, the AI will accurately summarize the data, but it won't know your team's specific safety rules. 
*   If you run it in a folder that already has a rule file (called `CLAUDE.md`), it won't delete your existing rules; it will safely suggest updates instead.

## Feature 2: Creating Strict Rules (`CLAUDE.md`)
This section proves why written rules are important.
*   **The Problem:** First, you ask the AI to plan a system upgrade based on its default knowledge. It will give an okay answer, but it will likely miss crucial steps or put files in the wrong places.
*   **The Fix:** You write a strict contract in the `CLAUDE.md` file that includes safety boundaries (like never logging into equipment directly), rules for proving its facts, and checklists for how files must be formatted.
*   **The Success:** When you ask for the upgrade plan again, the AI follows your strict rules, asks smart questions, and refuses to guess information it doesn't have.

## Feature 3: Plan Mode (The Safety Gate)
This shows how to stop the AI from making unapproved changes.
*   **The Problem:** Ask the AI to build an upgrade package, and it will immediately start writing files based on assumptions you never agreed to. Stop it before it gets too far and delete what it made.
*   **The Fix:** Turn on "Plan Mode" by pressing Shift+Tab. This puts the AI in a read-only mode where it must create a proposal for you to review before it touches any files. 
*   **The Success:** The AI will now point out missing evidence as a line item in its plan. You can argue with the AI about its plan, supply the missing data, and finally approve it while choosing to review every single edit manually.

## Feature 4: Creating a Skill
This shows how to save time on complex tasks.
*   You only create a "skill" *after* you have successfully completed a task once and want to make it repeatable.
*   You take the successful workflow you just finished and write it into a file called `SKILL.md`. 
*   Once saved, you can run that entire complex workflow in the future just by typing one short command.

## Feature 5: Security Hooks
This explains the difference between a polite suggestion and a strict security control.
*   **The Problem:** Point out that simply writing "do not log into servers" in a text file doesn't actually force the AI to obey; it might still try to do it or ask you for permission. 
*   **The Fix:** You write a "hook," which is a small script that actively watches what the AI is trying to do. You program it to instantly block any attempt to run commands like SSH or Telnet. 
*   **The Success:** When you ask the AI to log into a server again, the system physically blocks it. This proves that the rule is now an enforced security control.

## Feature 6: Subagents (Delegating Work)
This demonstrates how to keep the AI from getting overwhelmed.
*   **The Problem:** Having the AI read massive files fills up its memory (called the context window) very quickly, which makes its answers worse over time.
*   **The Fix:** You create a "subagent," which acts like a specialized junior assistant. 
*   **The Success:** You can send this subagent to read the massive files in the background. It will do the heavy lifting and report back with a short summary, keeping your main chat window clean and fast.

## Presentation Tips and Timing
*   The entire presentation should take exactly 60 minutes.
*   If you are running out of time, you can cut some of the backup examples, but you must never skip the steps where you "show the problem" first.
*   End the presentation by reminding the audience that everything built today is just text. Once your rules and methods are written down, they can be saved, reviewed by a team, and automatically enforced at any time of day.
