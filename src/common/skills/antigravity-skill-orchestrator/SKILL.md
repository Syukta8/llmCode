---
name: antigravity-skill-orchestrator
description: "A meta-skill that understands task requirements, dynamically selects appropriate skills from the local Claude skills library (~/.claude/skills) or Antigravity catalog, tracks combinations, and prevents skill overuse for simple tasks."
category: meta
risk: safe
source: community
tags: "[orchestration, meta-skill, task-evaluation, on-demand-skills]"
---

# antigravity-skill-orchestrator

## Overview

The `skill-orchestrator` coordinates on-demand skill execution without bloating the starting context. It evaluates task complexity before loading any skills. For simple tasks, it solves them directly with baseline tools. For complex tasks, it inspects and loads only the required specialized skills directly from the local Claude skills directory (`~/.claude/skills`).

## Core Rules

1. **Task Evaluation Guardrails (Simple vs Complex)**:
   - Contained/simple tasks (bug fixes, small edits, simple scripts, config updates): Solve directly with baseline file editing and bash tools. **DO NOT LOAD SPECIALIZED SKILLS**.
   - Complex/multi-domain tasks (architecture overhauls, formal security reviews, multi-agent pipelines): Proceed to step 2.

2. **On-Demand Skill Invocation From Claude Folder**:
   - The master local skills library resides at `~/.claude/skills` (`%USERPROFILE%\.claude\skills` on Windows).
   - Never load or scan the whole directory upfront.
   - When a specific domain skill is needed (e.g. `clean-code`, `systematic-debugging`, `testing-patterns`):
     - Read that skill's instruction file directly: `~/.claude/skills/<skill-name>/SKILL.md`.
     - Follow its instructions surgically for the current task.
     - Never modify, delete, or touch the skill files inside `~/.claude/skills`.

3. **Master Catalog Fallback**:
   - If a required skill is not found in `~/.claude/skills`, query `https://raw.githubusercontent.com/sickn33/agentic-awesome-skills/main/CATALOG.md`.
